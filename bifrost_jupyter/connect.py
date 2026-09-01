"""Kernel-side helper: ``from bifrost_jupyter import connect`` (design §3.3).

Returns a live Ray ``JobSubmissionClient`` pointed at the cluster's in-cluster
Ray head service. The address is derived from the cluster id and namespace
(``<id>-head-svc.<namespace>.svc:8265``) — no Bifrost call, and no auth header:
the per-owner NetworkPolicy admits the owner's notebook pod to the head service,
so reachability is the gate, not a token.

This works for in-cluster notebooks (the Nebari target). Remote/off-cluster
connect via the federating gateway is out of scope for the spike (it needs the
operator-configured gateway registry and a future owner-scoped address endpoint;
the existing registry list is Admin-only).

Ray is an optional dependency (``bifrost-jupyter[kernel]``) so it is imported
lazily — importing this module never requires Ray to be installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import _address
from .config import default_namespace

if TYPE_CHECKING:
    from ray.job_submission import JobSubmissionClient


def connect(cluster_id: str, *, namespace: str | None = None) -> JobSubmissionClient:
    """Return a ``JobSubmissionClient`` for ``cluster_id`` over the in-cluster Jobs API.

    ``namespace`` defaults to ``BIFROST_CLUSTER_NAMESPACE`` (else the built-in
    default). No credential is required for the in-cluster path.
    """
    namespace = namespace or default_namespace()
    address = _address.jobs_address(cluster_id, namespace)

    from ray.job_submission import JobSubmissionClient  # lazy: Ray is optional

    jsc: Any = JobSubmissionClient(address)
    return jsc
