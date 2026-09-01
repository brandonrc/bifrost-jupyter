"""Admin-approved cluster profiles → ``CreateCluster`` bodies.

The frozen Bifrost API has no user-facing "profile" object (design §5): the
server extension owns a curated allowlist of named shapes and maps a choice to a
``CreateCluster`` body so users never send raw manifests. The spike ships a
single hard-coded profile, ``"small"``.

Two invariants this module enforces (design §7):

* ``ttl_seconds`` is always set. An interactive cluster submits no gateway jobs,
  so ``idle_timeout_secs`` can never observe it as active and would never reap
  it; the absolute max-age cap (``ttl_seconds``) is the only reaper that can.
* ``owner`` is never set. Bifrost stamps the owner from the request identity
  ("never trusted from the client body", ``ClusterSpec.owner`` doc); sending it
  from the client body is at best ignored and at worst wrong.
"""

from __future__ import annotations

import os
import uuid

from bifrost_client.models.cluster_spec import ClusterSpec
from bifrost_client.models.create_cluster import CreateCluster
from bifrost_client.models.worker_group import WorkerGroup

# The one approved profile for the spike.
SMALL = "small"

# Default project when BIFROST_PROJECT is unset. `project` is a required
# ClusterSpec field; it is not an identity claim, so a client-side default is fine.
_DEFAULT_PROJECT = "jupyter"

# Ray version / image kept in lockstep — a single approved runtime for the spike.
_RAY_VERSION = "2.9.0"
_IMAGE = "rayproject/ray:2.9.0"

# 1 hour absolute cap: interactive clusters must be reaped even while "idle".
_TTL_SECONDS = 3600


def _generate_id(profile: str) -> str:
    """A stable, unique cluster id (also the gateway routing key / RayCluster name)."""
    return f"jl-{profile}-{uuid.uuid4().hex[:12]}"


def build_create_cluster(profile: str = SMALL, *, cluster_id: str | None = None) -> CreateCluster:
    """Expand an approved profile name into a ``CreateCluster`` request body.

    Raises ``KeyError`` for an unknown profile.
    """
    if profile != SMALL:
        raise KeyError(profile)

    cluster_id = cluster_id or _generate_id(profile)
    project = os.environ.get("BIFROST_PROJECT") or _DEFAULT_PROJECT

    spec = ClusterSpec(
        name=cluster_id,
        project=project,
        image=_IMAGE,
        ray_version=_RAY_VERSION,
        head_cpu="1",
        head_memory="2Gi",
        ttl_seconds=_TTL_SECONDS,
        # owner intentionally omitted — stamped control-plane-side from the token.
        worker_groups=[
            WorkerGroup(
                name="small-workers",
                cpu="1",
                memory="2Gi",
                replicas=1,
                min_replicas=1,
                max_replicas=2,
            )
        ],
    )
    return CreateCluster(id=cluster_id, spec=spec)
