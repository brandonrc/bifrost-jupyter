"""In-cluster Ray head-service address derivation (design §6; fix round 1).

The Jobs API address is derived *client-side* from the cluster id and namespace —
no Bifrost call. KubeRay names the head service ``<raycluster-name>-head-svc`` in
the cluster's namespace, and Bifrost uses the cluster id as the RayCluster name
(``CreateCluster.id`` doc), so a per-owner notebook pod reaches its cluster
directly at::

    http://<id>-head-svc.<namespace>.svc:8265      # Ray Jobs API
    ray://<id>-head-svc.<namespace>.svc:10001       # Ray Client (advanced)

The tier-2 per-owner NetworkPolicy (kuberay.go) admits the owner's notebook pod
to :8265 and :10001, so this in-cluster path needs **no** auth header — the
NetworkPolicy is the gate, not a token. (The Bearer token is only for Bifrost's
own control-plane calls: create/get/delete.)

Remote / off-cluster access is NOT covered here: it would need the operator-
configured gateway registry plus a future owner-scoped address endpoint (the
existing registry list is Admin-only). Out of scope for this spike — see README.
"""

from __future__ import annotations

JOBS_PORT = 8265
RAY_CLIENT_PORT = 10001


def head_service_host(cluster_id: str, namespace: str) -> str:
    """The in-cluster DNS name of the cluster's Ray head service."""
    return f"{cluster_id}-head-svc.{namespace}.svc"


def jobs_address(cluster_id: str, namespace: str) -> str:
    """The Ray Jobs API address (``http://...:8265``) for ``JobSubmissionClient``."""
    return f"http://{head_service_host(cluster_id, namespace)}:{JOBS_PORT}"


def ray_client_address(cluster_id: str, namespace: str) -> str:
    """The Ray Client address (``ray://...:10001``) — advanced/optional."""
    return f"ray://{head_service_host(cluster_id, namespace)}:{RAY_CLIENT_PORT}"


def connect_snippet(cluster_id: str, namespace: str) -> str:
    """A runnable ``JobSubmissionClient`` snippet — no auth header needed."""
    address = jobs_address(cluster_id, namespace)
    return (
        "from ray.job_submission import JobSubmissionClient\n"
        "\n"
        f'client = JobSubmissionClient("{address}")\n'
    )
