"""Kernel-side helper: ``from bifrost_jupyter import connect`` (design §3.3).

Returns a live Ray ``JobSubmissionClient`` pointed at the cluster's gateway Jobs
address, with the bearer auth header attached. Requirement #6 is inherently
kernel-side; this is the dask-gateway programmatic parallel.

Ray is an optional dependency (``bifrost-jupyter[kernel]``) so it is imported
lazily — importing this module never requires Ray to be installed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from . import _address
from .bifrost import BifrostClient, client_from_env

if TYPE_CHECKING:
    from ray.job_submission import JobSubmissionClient


class ClusterNotReachableError(RuntimeError):
    """Raised when the cluster has no gateway address registered yet."""


def connect(cluster_id: str, *, client: BifrostClient | None = None) -> JobSubmissionClient:
    """Return a ``JobSubmissionClient`` for ``cluster_id`` over the gateway Jobs API.

    Resolves the gateway host with the same server-side credential the extension
    uses, then attaches the bearer token (read from the environment) to the Ray
    client. Raises :class:`ClusterNotReachableError` if the cluster is not yet
    registered with the gateway.
    """
    client = client or client_from_env()

    host = client.gateway_host(cluster_id)
    if host is None:
        raise ClusterNotReachableError(
            f"cluster {cluster_id!r} has no gateway address registered yet"
        )

    token = os.environ[_address.TOKEN_ENV_VAR]
    headers = {"Authorization": f"Bearer {token}"}

    from ray.job_submission import JobSubmissionClient  # lazy: Ray is optional

    jsc: Any = JobSubmissionClient(_address.jobs_address(host), headers=headers)
    return jsc
