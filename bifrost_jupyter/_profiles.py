"""Admin-approved cluster profiles → ``CreateCluster`` bodies.

The frozen Bifrost API has no user-facing "profile" object (design §5): the
server extension owns a curated allowlist of named shapes and maps a choice to a
``CreateCluster`` body so users never send raw manifests (requirement #7,
"approved options, not arbitrary manifests"). The allowlist ships a sane default
set (:data:`DEFAULT_PROFILES`) and is overridable by deployment config
(``c.BifrostConfig.profiles`` — see :mod:`bifrost_jupyter.config`).

Two invariants this module enforces for *every* profile (design §7):

* ``ttl_seconds`` is always set. An interactive cluster submits no gateway jobs,
  so ``idle_timeout_secs`` can never observe it as active and would never reap
  it; the absolute max-age cap (``ttl_seconds``) is the only reaper that can.
* ``owner`` is never set. Bifrost stamps the owner from the request identity
  ("never trusted from the client body", ``ClusterSpec.owner`` doc); sending it
  from the client body is at best ignored and at worst wrong. Config profiles
  may not smuggle it in either — it is simply never read.

The client picks a profile by *name* only. No request field maps to a raw spec
field (no image/resources passthrough), so the allowlist is the sole path to a
``ClusterSpec``.

Profiles are config-driven. Hydrating shapes from ``GET /api/v1/pools`` /
``FlavorSpec`` is a deliberate non-goal for now: a pool/flavor describes node
scheduling and capacity (node labels, a flat resources dict, taints, cohorts),
not a curated cluster shape (head-vs-worker CPU/memory split, autoscaling
bounds, ``ray_version``, image), so the mapping would be lossy and complex.
Left as a follow-up.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from bifrost_client.models.cluster_spec import ClusterSpec
from bifrost_client.models.create_cluster import CreateCluster
from bifrost_client.models.worker_group import WorkerGroup

# The default first-choice profile (kept for the panel's default selection and
# for callers that don't specify one).
SMALL = "small"

# Default project when BIFROST_PROJECT is unset. `project` is a required
# ClusterSpec field; it is not an identity claim, so a client-side default is fine.
_DEFAULT_PROJECT = "jupyter"

# 1 hour absolute cap: interactive clusters must be reaped even while "idle".
# Used as the fallback when a config profile omits ttl_seconds (keeping the
# "ttl always set" invariant true regardless of deployment config).
_DEFAULT_TTL_SECONDS = 3600

# KubeRay truncates the head-service name if the RayCluster name exceeds 41
# chars, which would break the derived in-cluster connect address. Keep the
# generated id well under that: `jl-<slug>-<12 hex>`. The 12-hex suffix + the
# `jl-` prefix + two hyphens cost 17 chars, so the slug is capped so the whole
# id stays within _MAX_ID_LEN.
_MAX_ID_LEN = 40
_ID_SUFFIX_HEX = 12
_MAX_SLUG_LEN = _MAX_ID_LEN - len("jl-") - len("-") - _ID_SUFFIX_HEX  # 24


@dataclass(frozen=True)
class WorkerGroupShape:
    """One approved worker group in a profile (a subset of ``WorkerGroup``)."""

    name: str
    cpu: str
    memory: str
    replicas: int
    min_replicas: int
    max_replicas: int
    gpu: str | None = None


@dataclass(frozen=True)
class Profile:
    """An admin-approved cluster shape. Not user-supplied; only its name is."""

    name: str
    description: str
    image: str
    ray_version: str
    head_cpu: str
    head_memory: str
    ttl_seconds: int
    worker_groups: tuple[WorkerGroupShape, ...]


@dataclass(frozen=True)
class ProfileView:
    """The safe, user-facing view of a profile.

    Deliberately excludes the image, ``ray_version``, and any raw manifest
    surface — only the coarse resource shape a user needs to choose from.
    """

    name: str
    description: str
    head_cpu: str
    head_memory: str
    workers: tuple[dict, ...]
    gpu: int  # total GPUs at max scale (coarse "is this a GPU profile?")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "head_cpu": self.head_cpu,
            "head_memory": self.head_memory,
            "workers": [dict(w) for w in self.workers],
            "gpu": self.gpu,
        }


class UnknownProfileError(KeyError):
    """Raised when a requested profile name is not in the allowlist.

    Subclasses ``KeyError`` so existing ``pytest.raises(KeyError)`` callers keep
    working, while carrying a clear, safe message (the available names are
    already public via ``GET /bifrost/profiles``).
    """

    def __init__(self, name: str, available: Iterable[str] = ()) -> None:
        self.name = name
        self.available = sorted(available)
        super().__init__(name)

    def __str__(self) -> str:
        return f"unknown profile {self.name!r}; choose one of {self.available}"


# ---- Default allowlist ------------------------------------------------------
# A single approved runtime for the shipped defaults: image and ray_version kept
# in lockstep. A deployment override supplies its own per-profile image.
_RAY_VERSION = "2.9.0"
_IMAGE = "rayproject/ray:2.9.0"

DEFAULT_PROFILES: dict[str, Profile] = {
    SMALL: Profile(
        name=SMALL,
        description="1 CPU head + up to 2 small CPU workers (1 CPU / 2Gi each).",
        image=_IMAGE,
        ray_version=_RAY_VERSION,
        head_cpu="1",
        head_memory="2Gi",
        ttl_seconds=_DEFAULT_TTL_SECONDS,
        worker_groups=(
            WorkerGroupShape(
                name="workers",
                cpu="1",
                memory="2Gi",
                replicas=1,
                min_replicas=1,
                max_replicas=2,
            ),
        ),
    ),
    "medium": Profile(
        name="medium",
        description="2 CPU head + 2-4 CPU workers (2 CPU / 8Gi each).",
        image=_IMAGE,
        ray_version=_RAY_VERSION,
        head_cpu="2",
        head_memory="8Gi",
        ttl_seconds=_DEFAULT_TTL_SECONDS,
        worker_groups=(
            WorkerGroupShape(
                name="workers",
                cpu="2",
                memory="8Gi",
                replicas=2,
                min_replicas=2,
                max_replicas=4,
            ),
        ),
    ),
    "gpu": Profile(
        name="gpu",
        description="2 CPU head + up to 2 GPU workers (4 CPU / 16Gi / 1 GPU each).",
        image=_IMAGE,
        ray_version=_RAY_VERSION,
        head_cpu="2",
        head_memory="8Gi",
        ttl_seconds=_DEFAULT_TTL_SECONDS,
        worker_groups=(
            WorkerGroupShape(
                name="gpu-workers",
                cpu="4",
                memory="16Gi",
                replicas=1,
                min_replicas=1,
                max_replicas=2,
                gpu="1",
            ),
        ),
    ),
}


def _slugify(name: str) -> str:
    """A DNS-1035-safe, length-bounded slug for use inside the RayCluster name.

    Lowercases, collapses runs of invalid chars to a single hyphen, strips
    leading/trailing hyphens, and truncates so the generated id never exceeds
    ``_MAX_ID_LEN`` (KubeRay head-service truncation guard).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:_MAX_SLUG_LEN].strip("-")
    return slug or "cluster"


def _generate_id(profile: str) -> str:
    """A stable, unique cluster id (also the gateway routing key / RayCluster name)."""
    return f"jl-{_slugify(profile)}-{uuid.uuid4().hex[:_ID_SUFFIX_HEX]}"


def _total_gpus(profile: Profile) -> int:
    """Coarse GPU count at max scale, for the safe view."""
    total = 0
    for wg in profile.worker_groups:
        if wg.gpu:
            try:
                total += int(wg.gpu) * wg.max_replicas
            except ValueError:
                # Non-integer GPU request (e.g. a sharing fraction); treat as present.
                total += wg.max_replicas
    return total


def list_profiles(profiles: dict[str, Profile] | None = None) -> list[ProfileView]:
    """Return the safe, user-facing view of every approved profile.

    The view carries only the coarse shape (head CPU/memory, worker CPU/memory/
    GPU and replica bounds) — never the image, ``ray_version``, or any raw
    manifest field.
    """
    profiles = profiles if profiles is not None else DEFAULT_PROFILES
    return [
        ProfileView(
            name=p.name,
            description=p.description,
            head_cpu=p.head_cpu,
            head_memory=p.head_memory,
            workers=tuple(
                {
                    "cpu": wg.cpu,
                    "memory": wg.memory,
                    "gpu": wg.gpu,
                    "min_replicas": wg.min_replicas,
                    "max_replicas": wg.max_replicas,
                }
                for wg in p.worker_groups
            ),
            gpu=_total_gpus(p),
        )
        for p in profiles.values()
    ]


def profile_to_spec(
    name: str,
    profiles: dict[str, Profile] | None = None,
    *,
    cluster_id: str | None = None,
) -> CreateCluster:
    """Map an approved profile *name* to a ``CreateCluster`` request body.

    Raises :class:`UnknownProfileError` for a name not in the allowlist — never
    falls back to a default. The client supplies only the name; every spec field
    comes from the approved profile.
    """
    profiles = profiles if profiles is not None else DEFAULT_PROFILES
    try:
        profile = profiles[name]
    except KeyError:
        raise UnknownProfileError(name, profiles.keys()) from None

    cluster_id = cluster_id or _generate_id(name)
    project = os.environ.get("BIFROST_PROJECT") or _DEFAULT_PROJECT

    spec = ClusterSpec(
        name=cluster_id,
        project=project,
        image=profile.image,
        ray_version=profile.ray_version,
        head_cpu=profile.head_cpu,
        head_memory=profile.head_memory,
        ttl_seconds=profile.ttl_seconds,
        # owner intentionally omitted — stamped control-plane-side from the token.
        worker_groups=[
            WorkerGroup(
                name=wg.name,
                cpu=wg.cpu,
                memory=wg.memory,
                gpu=wg.gpu,
                replicas=wg.replicas,
                min_replicas=wg.min_replicas,
                max_replicas=wg.max_replicas,
            )
            for wg in profile.worker_groups
        ],
    )
    return CreateCluster(id=cluster_id, spec=spec)


def build_create_cluster(profile: str = SMALL, *, cluster_id: str | None = None) -> CreateCluster:
    """Backward-compatible wrapper mapping a default-set profile to a body.

    Prefer :func:`profile_to_spec`, which accepts a deployment-configured
    allowlist. Retained for callers pinned to the built-in defaults.
    """
    return profile_to_spec(profile, DEFAULT_PROFILES, cluster_id=cluster_id)


_REQUIRED_PROFILE_KEYS = ("name", "image", "ray_version", "head_cpu", "head_memory")
_REQUIRED_WORKER_KEYS = ("name", "cpu", "memory", "replicas", "min_replicas", "max_replicas")


def _parse_worker_group(raw: dict, profile_name: str) -> WorkerGroupShape:
    missing = [k for k in _REQUIRED_WORKER_KEYS if raw.get(k) in (None, "")]
    if missing:
        raise ValueError(
            f"profile {profile_name!r} worker group is missing required "
            f"field(s): {', '.join(missing)}"
        )
    return WorkerGroupShape(
        name=str(raw["name"]),
        cpu=str(raw["cpu"]),
        memory=str(raw["memory"]),
        replicas=int(raw["replicas"]),
        min_replicas=int(raw["min_replicas"]),
        max_replicas=int(raw["max_replicas"]),
        gpu=str(raw["gpu"]) if raw.get("gpu") not in (None, "") else None,
    )


def _parse_profile(raw: dict) -> Profile:
    missing = [k for k in _REQUIRED_PROFILE_KEYS if raw.get(k) in (None, "")]
    if missing:
        label = raw.get("name", "<unnamed>")
        raise ValueError(
            f"profile {label!r} is missing required field(s): {', '.join(missing)}"
        )
    worker_groups = raw.get("worker_groups") or []
    if not worker_groups:
        raise ValueError(f"profile {raw['name']!r} must define at least one worker group")
    return Profile(
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        image=str(raw["image"]),
        ray_version=str(raw["ray_version"]),
        head_cpu=str(raw["head_cpu"]),
        head_memory=str(raw["head_memory"]),
        # ttl_seconds is ALWAYS set: honour the config value, else the safe cap.
        # An 'owner' key, if present, is intentionally ignored (never read).
        ttl_seconds=int(raw.get("ttl_seconds") or _DEFAULT_TTL_SECONDS),
        worker_groups=tuple(_parse_worker_group(wg, str(raw["name"])) for wg in worker_groups),
    )


def resolve_profiles(config_profiles: object) -> dict[str, Profile]:
    """Resolve the effective allowlist from deployment config.

    An empty/unset config yields the built-in :data:`DEFAULT_PROFILES`. A
    non-empty config *replaces* the defaults entirely (a deployment curates its
    own allowlist). Raises ``ValueError`` on malformed config so a misconfigured
    deployment fails loudly at extension load rather than at request time.
    """
    if not config_profiles:
        return dict(DEFAULT_PROFILES)
    if not isinstance(config_profiles, (list, tuple)):
        raise ValueError("BifrostConfig.profiles must be a list of profile dicts")
    resolved: dict[str, Profile] = {}
    for raw in config_profiles:
        if not isinstance(raw, dict):
            raise ValueError("each BifrostConfig.profiles entry must be a dict")
        profile = _parse_profile(raw)
        if profile.name in resolved:
            raise ValueError(f"duplicate profile name in config: {profile.name!r}")
        resolved[profile.name] = profile
    return resolved
