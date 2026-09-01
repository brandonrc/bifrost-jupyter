"""Gateway Jobs-address helpers shared by the server handler and the kernel helper.

The federating gateway routes to a cluster by the request *Host* header: each
registered cluster is exposed at its own ``hostname`` (``internal/core/registry.go``:
"the cluster identity must live in the host, not the path"). So a Ray
``JobSubmissionClient`` reaches a cluster by pointing its address at that host over
HTTPS; the gateway strips the caller's own bearer credential and injects the
cluster's static Ray token southbound (ADR-0003).

The bearer token is deliberately *never* embedded in any string these helpers
return. The generated snippet reads it from the ``BIFROST_TOKEN`` environment
variable at runtime, so an address payload can be logged or shown in a notebook
without leaking the credential.
"""

from __future__ import annotations

# Env var the kernel-side client reads the bearer token from. Keeping the name
# (not the value) in snippets/hints is safe.
TOKEN_ENV_VAR = "BIFROST_TOKEN"


def jobs_address(host: str) -> str:
    """Return the ``https://<host>`` Jobs API address for a gateway hostname."""
    return f"https://{host}"


def headers_hint() -> dict[str, str]:
    """A non-secret description of the auth header the client must send.

    The value is a placeholder naming the env var, never the token itself.
    """
    return {"Authorization": f"Bearer ${{{TOKEN_ENV_VAR}}}"}


def connect_snippet(host: str) -> str:
    """A runnable ``JobSubmissionClient`` snippet that reads the token from env."""
    address = jobs_address(host)
    return (
        "import os\n"
        "from ray.job_submission import JobSubmissionClient\n"
        "\n"
        "client = JobSubmissionClient(\n"
        f'    "{address}",\n'
        "    headers={\"Authorization\": f\"Bearer {os.environ['" + TOKEN_ENV_VAR + "']}\"},\n"
        ")\n"
    )
