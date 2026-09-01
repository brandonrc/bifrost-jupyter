"""In-cluster Ray Jobs REST client — requirement #11's env vars (design §2, §6).

``ClusterSpec`` has no ``env``/``runtime_env`` field, so #11's env vars attach to
a Ray **job** at submit time as ``runtime_env.env_vars`` (design §2, locked).
That is a Ray-side contract, not a Bifrost one: this module speaks the raw Ray
Jobs REST API on the cluster's head service.

**No auth header, deliberately.** Unlike every Bifrost control-plane call, this
path does not go through Bifrost at all — the jupyter-server extension runs
inside the user's notebook pod, which carries the ``bifrost.dev/owner`` label,
and the tier-2 per-owner NetworkPolicy (``kuberay.go``) admits exactly that pod
to the head service on :8265. There is no bearer token on this path and none must
be added.

What stands in for one is *two* things, not one: the NetworkPolicy's reachability
**and** a cluster id validated by :func:`bifrost_jupyter._address.validate_cluster_id`.
Reachability alone is not authorization — the id is a caller-controlled path
segment interpolated into the host, so an unvalidated one would let the caller
choose which host this server connects to, from inside the cluster network. The
id check is what pins the target to a head service in the configured namespace;
the NetworkPolicy is what makes reaching it legitimate.

**No Ray SDK.** ``ray`` stays the optional ``[kernel]`` extra (the per-user
server pod is not bloated with it), so the wire contract is spoken directly over
tornado's ``AsyncHTTPClient`` — already a dependency via jupyter-server, and
non-blocking inside a tornado handler.

Wire contract (Ray Jobs REST API, verified against the Ray docs and
``ray/dashboard/modules/job/{common,sdk}.py``):

* ``POST <jobs_address>/api/jobs/`` — the trailing slash is required.
  Body is ``JobSubmitRequest``: ``entrypoint`` (str, required), plus optional
  ``submission_id``, ``runtime_env``, ``metadata``, ``entrypoint_num_cpus`` …
  Response is ``JobSubmitResponse``: ``{"job_id": …, "submission_id": …}``;
  ``submission_id`` is the id every later call keys on (``job_id`` is the
  deprecated older name, kept as a fallback for older Ray).
* ``GET <jobs_address>/api/jobs/{submission_id}`` — response is ``JobDetails``
  (``status``, ``message``, ``start_time``, ``end_time``, …).

  https://docs.ray.io/en/latest/cluster/running-applications/job-submission/rest.html

Errors are reduced to a status + fixed safe message the same way
:mod:`bifrost_jupyter.bifrost` does for the control plane — an unreachable or
unhappy cluster becomes a mapped 4xx/502, never an unhandled 500.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest, HTTPResponse

#: Ray's job server rejects ``/api/jobs`` without the trailing slash.
SUBMIT_PATH = "/api/jobs/"

CONNECT_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 30.0

#: Fields of Ray's ``JobDetails`` the panel is allowed to see. Everything else
#: (driver_info, driver_agent_http_address, node ids, the echoed runtime_env …)
#: is dropped: the browser gets a small, stable view, not a passthrough.
_STATUS_FIELDS = ("status", "message", "start_time", "end_time")


class RayJobsError(RuntimeError):
    """A Ray Jobs API failure reduced to a status + safe message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# Fixed, non-leaking messages per upstream status; the upstream body is discarded.
_SAFE_MESSAGES = {
    400: "invalid job request",
    404: "job not found",
    405: "ray jobs api not available on this cluster",
}
_DEFAULT_MESSAGE = "ray jobs request failed"
_UNREACHABLE_MESSAGE = "ray cluster unreachable"


def _translate(exc: HTTPClientError) -> RayJobsError:
    status = exc.code
    # tornado reports connect failures/timeouts as the synthetic code 599.
    if status == 599:
        return RayJobsError(502, _UNREACHABLE_MESSAGE)
    if status >= 500:
        return RayJobsError(502, "ray cluster error")
    return RayJobsError(status, _SAFE_MESSAGES.get(status, _DEFAULT_MESSAGE))


def clean_env_vars(raw: Any) -> dict[str, str]:
    """Validate the panel's ``env_vars`` map (requirement #11) or raise ``ValueError``.

    Ray requires ``runtime_env.env_vars`` to be a flat ``str -> str`` map; a
    non-string value would fail deep inside the job server (or be coerced
    surprisingly), so it is rejected here as a clean 4xx instead.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("invalid 'env_vars'")

    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError("invalid 'env_vars'")
        # bool is a subclass of int, not str, so True/1 are rejected here too.
        if not isinstance(value, str):
            raise ValueError("invalid 'env_vars'")
        cleaned[key] = value
    return cleaned


def build_submit_body(entrypoint: str, env_vars: dict[str, str]) -> dict[str, Any]:
    """The ``JobSubmitRequest`` body: this is where #11's env vars land.

    ``runtime_env`` is always sent (an empty ``env_vars`` map is a no-op for Ray)
    so the wire shape is the same whether or not the user added variables.
    """
    return {"entrypoint": entrypoint, "runtime_env": {"env_vars": dict(env_vars)}}


def _decode(response: HTTPResponse) -> dict[str, Any]:
    try:
        payload = json.loads(response.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RayJobsError(502, "invalid response from ray cluster") from None
    if not isinstance(payload, dict):
        raise RayJobsError(502, "invalid response from ray cluster")
    return payload


async def _fetch(request: HTTPRequest) -> dict[str, Any]:
    client = AsyncHTTPClient()
    try:
        response = await client.fetch(request)
    except HTTPClientError as exc:
        raise _translate(exc) from None
    except Exception:
        # DNS failure, connection refused, TLS/socket errors: tornado lets these
        # through raw. The cluster may simply not be up yet — map to a graceful
        # 502 rather than letting the handler 500.
        raise RayJobsError(502, _UNREACHABLE_MESSAGE) from None
    return _decode(response)


async def submit_job(
    jobs_address: str, entrypoint: str, env_vars: dict[str, str]
) -> dict[str, str]:
    """Submit a Ray job; return ``{"job_id": …, "submission_id": …}``.

    ``jobs_address`` is the in-cluster head-service URL from
    :func:`bifrost_jupyter._address.jobs_address`. No auth header (see module
    docstring).
    """
    request = HTTPRequest(
        jobs_address + SUBMIT_PATH,
        method="POST",
        body=json.dumps(build_submit_body(entrypoint, env_vars)),
        headers={"Content-Type": "application/json"},
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
    )
    payload = await _fetch(request)

    # ``submission_id`` is the current field; ``job_id`` is its deprecated older
    # name, still returned by Ray and used by pre-2.x servers.
    submission_id = payload.get("submission_id") or payload.get("job_id")
    if not isinstance(submission_id, str) or not submission_id:
        raise RayJobsError(502, "invalid response from ray cluster")
    return {"job_id": submission_id, "submission_id": submission_id}


async def get_job(jobs_address: str, job_id: str) -> dict[str, Any]:
    """Fetch one job's status: the allowlisted view of Ray's ``JobDetails``."""
    request = HTTPRequest(
        # ``quote`` with no safe characters keeps a hostile id (``../..``) from
        # escaping the /api/jobs/ path.
        jobs_address + SUBMIT_PATH + quote(job_id, safe=""),
        method="GET",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
    )
    payload = await _fetch(request)

    view: dict[str, Any] = {"job_id": job_id}
    for field in _STATUS_FIELDS:
        if field in payload:
            view[field] = payload[field]
    return view
