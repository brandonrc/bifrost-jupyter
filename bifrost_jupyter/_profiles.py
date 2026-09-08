"""The administrator's profile catalog → ``CreateCluster`` bodies.

Users never send a raw manifest. They pick a profile by *name*, and the name is
all the client body carries for the shape (requirement #7: "approved options,
not arbitrary manifests"). Bifrost owns the catalog — ``GET /api/v1/profiles``
returns the profiles the caller's projects may use, and ``ClusterSpec.profile``
tells the control plane to fill the shape from it, refusing any conflicting
field in the body.

This module used to own a catalog of its own: a built-in set on
``rayproject/ray:2.9.0`` (Python 3.8), overridable per deployment through a
traitlet nobody set. It was written before Bifrost had profiles, and it kept
working after — the panel offered its shapes, the control plane accepted them —
right up to the moment a notebook on Ray 2.56 / Python 3.12 called
``ray.init()`` into the cluster it had just started and was refused with a
version mismatch. Two catalogs is one too many: the administrator's is the one
that is curated, project-scoped and audited, so it is the only one now.

What stays here is what is still the client's business: a stable, DNS-safe id
for the new cluster, and the safe *view* of a profile (its coarse shape, never
its image or ``ray_version``), which is what the panel renders.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from bifrost_client.models.cluster_spec import ClusterSpec
from bifrost_client.models.create_cluster import CreateCluster
from bifrost_client.models.profile_spec import ProfileSpec

# KubeRay truncates the head-service name if the RayCluster name exceeds 41
# chars, which would break the derived in-cluster connect address. Keep the
# generated id well under that: `jl-<slug>-<12 hex>`. The 12-hex suffix + the
# `jl-` prefix + two hyphens cost 17 chars, so the slug is capped so the whole
# id stays within _MAX_ID_LEN.
_MAX_ID_LEN = 40
_ID_SUFFIX_HEX = 12
_MAX_SLUG_LEN = _MAX_ID_LEN - len("jl-") - len("-") - _ID_SUFFIX_HEX  # 24


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
    """Raised when a requested profile name is not in the caller's catalog.

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


def _total_gpus(profile: ProfileSpec) -> int:
    """Coarse GPU count at max scale, for the safe view."""
    total = 0
    for wg in profile.worker_groups or ():
        if wg.gpu:
            try:
                total += int(wg.gpu) * wg.max_replicas
            except ValueError:
                # Non-integer GPU request (e.g. a sharing fraction); treat as present.
                total += wg.max_replicas
    return total


def list_profiles(catalog: Iterable[ProfileSpec]) -> list[ProfileView]:
    """The safe, user-facing view of every profile in the caller's catalog.

    The view carries only the coarse shape (head CPU/memory, worker CPU/memory/
    GPU and replica bounds) — never the image, ``ray_version``, or any raw
    manifest field.
    """
    return [
        ProfileView(
            name=p.name,
            description=p.description or "",
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
                for wg in (p.worker_groups or ())
            ),
            gpu=_total_gpus(p),
        )
        for p in catalog
    ]


def profile_to_spec(
    name: str,
    catalog: Iterable[ProfileSpec],
    *,
    cluster_id: str | None = None,
    project: str | None = None,
) -> CreateCluster:
    """Map a profile *name* from the caller's catalog to a ``CreateCluster`` body.

    Raises :class:`UnknownProfileError` for a name the catalog does not carry —
    never falls back to a default. The body names the profile and leaves every
    shape field empty; Bifrost fills them from its catalog and refuses a body
    that tries to say otherwise, so nothing a client sends can widen a profile.

    ``ttl_seconds`` and ``idle_timeout_secs`` are the catalog's too. This module
    used to force a TTL on every cluster because an interactive cluster submits
    no gateway jobs and would never be reaped by idleness; that reasoning now
    lives with the administrator who writes the profile, where it can be seen.

    ``project`` is settled by the caller (``_projects.resolve``) and passed in.
    """
    names = [p.name for p in catalog]
    if name not in names:
        raise UnknownProfileError(name, names)

    cluster_id = cluster_id or _generate_id(name)
    if not project:
        raise ValueError("profile_to_spec needs a project; resolve one first")

    spec = ClusterSpec(
        name=cluster_id,
        project=project,
        profile=name,
        # Empty on purpose: "zero-valued fields are filled from the profile".
        image="",
        ray_version="",
        head_cpu="",
        head_memory="",
        worker_groups=[],
        # owner intentionally omitted — stamped control-plane-side from the token.
    )
    return CreateCluster(id=cluster_id, spec=spec)
