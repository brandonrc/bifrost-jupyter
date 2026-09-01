"""Thin server-side wrapper over the published ``bifrost_client`` (design §3.2).

Holds the user credential server-side and attaches it as ``Authorization: Bearer``
to every Bifrost call. The credential is read from the environment only
(``BIFROST_TOKEN``, a ``mob_`` PAT for the spike; OIDC passthrough is Task 9) and
is never returned to the browser or written to a response body.

Upstream ``ApiException``\\s are translated to :class:`BifrostAPIError`, which
carries only the HTTP status and a fixed, safe message — never the upstream
response body, which could echo internal detail or the token.
"""

from __future__ import annotations

import os

from bifrost_client import ApiClient, ApiException, ClustersApi, Configuration, RegistryApi
from bifrost_client.models.cluster_view import ClusterView
from bifrost_client.models.create_cluster import CreateCluster

API_URL_ENV_VAR = "BIFROST_API_URL"
TOKEN_ENV_VAR = "BIFROST_TOKEN"


class BifrostConfigError(RuntimeError):
    """Raised when required server-side configuration (URL/token) is missing."""


class BifrostAPIError(RuntimeError):
    """A Bifrost control-plane error reduced to a status + safe message.

    Deliberately does not carry the upstream response body.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# Fixed, non-leaking messages per upstream status. The upstream body is discarded.
_SAFE_MESSAGES = {
    400: "invalid request",
    401: "unauthorized",
    403: "forbidden",
    404: "not found",
    409: "conflict",
    422: "invalid cluster specification",
}
_DEFAULT_MESSAGE = "bifrost request failed"


def _translate(exc: ApiException) -> BifrostAPIError:
    status = exc.status if isinstance(exc.status, int) else 502
    # Never surface a raw upstream 5xx status to the browser as-is beyond 502.
    if status >= 500:
        return BifrostAPIError(502, "bifrost upstream error")
    return BifrostAPIError(status, _SAFE_MESSAGES.get(status, _DEFAULT_MESSAGE))


class BifrostClient:
    """A small facade exposing exactly the calls the spike needs."""

    def __init__(self, api_url: str, token: str) -> None:
        config = Configuration(host=api_url, access_token=token)
        api_client = ApiClient(config)
        self._clusters = ClustersApi(api_client)
        self._registry = RegistryApi(api_client)

    def create_cluster(self, body: CreateCluster) -> None:
        try:
            self._clusters.create_cluster(body)
        except ApiException as exc:
            raise _translate(exc) from None

    def get_cluster(self, cluster_id: str) -> ClusterView:
        try:
            return self._clusters.get_cluster(cluster_id)
        except ApiException as exc:
            raise _translate(exc) from None

    def delete_cluster(self, cluster_id: str) -> None:
        try:
            self._clusters.delete_cluster(cluster_id)
        except ApiException as exc:
            raise _translate(exc) from None

    def gateway_host(self, cluster_id: str) -> str | None:
        """Resolve the gateway hostname the cluster is exposed at, or ``None``.

        The gateway routes by Host header, so this hostname is what a
        ``JobSubmissionClient`` address must point at.
        """
        try:
            entries = self._registry.list_registry()
        except ApiException as exc:
            raise _translate(exc) from None
        for entry in entries:
            if entry.id == cluster_id:
                return entry.hostname
        return None


def client_from_env() -> BifrostClient:
    """Construct a :class:`BifrostClient` from ``BIFROST_API_URL`` + ``BIFROST_TOKEN``.

    Raises :class:`BifrostConfigError` if either is unset.
    """
    api_url = os.environ.get(API_URL_ENV_VAR)
    token = os.environ.get(TOKEN_ENV_VAR)
    if not api_url:
        raise BifrostConfigError(f"{API_URL_ENV_VAR} is not set")
    if not token:
        raise BifrostConfigError(f"{TOKEN_ENV_VAR} is not set")
    return BifrostClient(api_url, token)
