"""In-cluster Ray head-service address derivation (design §6; fix round 1).

The Jobs API address is derived *client-side* from the cluster id and namespace —
no Bifrost call. KubeRay names the head service ``<raycluster-name>-head-svc`` in
the cluster's namespace, and Bifrost uses the cluster id as the RayCluster name
(``CreateCluster.id`` doc), so a per-owner notebook pod reaches its cluster
directly at::

    http://<id>-head-svc.<namespace>.svc:8265      # Ray Jobs API *and* dashboard
    ray://<id>-head-svc.<namespace>.svc:10001       # Ray Client (advanced)

The authorization story on this path has **two** halves, and both are load-bearing:

1. The tier-2 per-owner NetworkPolicy (kuberay.go) admits the owner's notebook
   pod to :8265 and :10001, so no auth header is needed — the NetworkPolicy is
   the gate, not a token. (The Bearer token is only for Bifrost's own
   control-plane calls: create/get/delete.)
2. The cluster id is **validated** (:func:`validate_cluster_id`) before it is
   interpolated into any host, so the target is pinned to a head service in the
   configured namespace. Without that, half 1 means nothing: an id is a path
   segment a caller fully controls, and an unvalidated one lets the caller pick
   the host the *server* connects to — from inside the cluster network, where the
   NetworkPolicy is not the constraint the browser's reachability was.

Remote / off-cluster access is NOT covered here: it would need the operator-
configured gateway registry plus a future owner-scoped address endpoint (the
existing registry list is Admin-only). Out of scope for this spike — see README.
"""

from __future__ import annotations

import re

JOBS_PORT = 8265
RAY_CLIENT_PORT = 10001

#: A cluster id becomes a Kubernetes Service name (``<id>-head-svc``) and a DNS
#: label inside every address below, so it is constrained to a single RFC 1123
#: DNS label: lowercase alphanumerics and hyphens, no leading/trailing hyphen,
#: 1-63 characters. ``_profiles._generate_id`` produces ``jl-<slug>-<12 hex>``,
#: which is comfortably inside this.
#:
#: Being a *single label* is the security-relevant part, not the cosmetics: it
#: admits no ``.``, ``:``, ``?``, ``#``, ``@``, ``%``, ``/``, whitespace or
#: uppercase, which is exactly the set that would let an id restructure the URL
#: it is interpolated into (``evil.example:9999?`` would otherwise make
#: ``http://evil.example:9999?-head-svc.<ns>.svc:8265`` — an attacker-chosen
#: host, with the intended suffix swallowed into the query string).
CLUSTER_ID_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"

_CLUSTER_ID_RE = re.compile(CLUSTER_ID_PATTERN)


class InvalidClusterIdError(ValueError):
    """A cluster id that is not a single RFC 1123 DNS label."""


def validate_cluster_id(cluster_id: str) -> str:
    """Return ``cluster_id`` if it is a well-formed id, else raise.

    This is the single chokepoint for the shape of a cluster id. Every route that
    takes an ``{id}`` path segment must run it through here (handlers do so up
    front, to answer a clean 400), and :func:`head_service_host` calls it again so
    no address can be derived from an unchecked id even if a future route forgets.
    """
    if not isinstance(cluster_id, str) or not _CLUSTER_ID_RE.match(cluster_id):
        raise InvalidClusterIdError("invalid cluster id")
    return cluster_id


def head_service_host(cluster_id: str, namespace: str) -> str:
    """The in-cluster DNS name of the cluster's Ray head service.

    Raises :class:`InvalidClusterIdError` for an id that is not a single DNS
    label — see :func:`validate_cluster_id`. This is the backstop, not the
    primary check: handlers validate first so the caller gets a 400 rather than
    a 500.
    """
    validate_cluster_id(cluster_id)
    return f"{cluster_id}-head-svc.{namespace}.svc"


def jobs_address(cluster_id: str, namespace: str) -> str:
    """The Ray Jobs API address (``http://...:8265``) for ``JobSubmissionClient``."""
    return f"http://{head_service_host(cluster_id, namespace)}:{JOBS_PORT}"


def dashboard_address(cluster_id: str, namespace: str) -> str:
    """The Ray **dashboard** origin for a cluster.

    Ray serves the dashboard single-page app and the Jobs REST API from the same
    aiohttp server on the same port (:8265) — the dashboard UI is at ``/`` and
    the Jobs API under ``/api/jobs/``. So this is deliberately the same string as
    :func:`jobs_address`; it exists as its own name because the two are separate
    *concerns* (observability vs. job submission) even though they share a port,
    and a reader of ``handlers.py`` should not have to know that coincidence.

    Returned with no trailing slash: the dashboard proxy appends the sub-path.
    """
    return jobs_address(cluster_id, namespace)


def ray_client_address(cluster_id: str, namespace: str) -> str:
    """The Ray Client address (``ray://...:10001``) — advanced/optional."""
    return f"ray://{head_service_host(cluster_id, namespace)}:{RAY_CLIENT_PORT}"


def connect_snippet(cluster_id: str, namespace: str) -> str:
    """A runnable ``JobSubmissionClient`` snippet — no auth header needed.

    The Ray Client (``ray://…:10001``, gRPC) path is offered only as a commented
    *advanced* alternative: it is reachable **only** from the cluster owner's
    notebook pod (the per-owner NetworkPolicy gates :10001), so it is not the
    default. The Jobs API line above is the recommended, remotely-usable path.
    """
    address = jobs_address(cluster_id, namespace)
    ray_client = ray_client_address(cluster_id, namespace)
    return (
        "from ray.job_submission import JobSubmissionClient\n"
        "\n"
        f'client = JobSubmissionClient("{address}")\n'
        "\n"
        "# Advanced (in-cluster, owner-pod-only) alternative: the Ray Client gRPC\n"
        "# endpoint is reachable only from the cluster owner's notebook pod, gated\n"
        "# by the per-owner NetworkPolicy (no auth header needed there).\n"
        "# import ray\n"
        f'# ray.init("{ray_client}")\n'
    )
