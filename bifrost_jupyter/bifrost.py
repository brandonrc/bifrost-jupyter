"""Thin server-side wrapper over the published ``bifrost_client`` (design §3.2).

Holds the user credential server-side and attaches it as ``Authorization: Bearer``
to every Bifrost call. The credential itself is resolved by
:mod:`bifrost_jupyter._credentials` (design §4: OIDC access token from the pod
env → optional RFC 8693 exchange → optional session PAT, with the ``bfr_`` PAT in
``BIFROST_TOKEN`` as the dev fallback) and is never returned to the browser or
written to a response body.

Two behaviours here exist because credentials expire:

* a Bifrost ``401`` is retried **once** against a freshly resolved credential, so
  an access token that died mid-session is renewed instead of surfacing to the
  user as an unexplained failure;
* a ``403`` on a lifecycle call carries the actionable reason — cluster
  create/stop/suspend/resume need Write on ``cluster``, i.e. Bifrost's
  ``operator`` role, which a Nebari user only holds if their IdP group is mapped
  to it (see the README).

Every call here is **synchronous and blocking** — the generated client is
urllib3-based. Handlers must therefore never call these directly from tornado's
IOLoop thread; :mod:`bifrost_jupyter.handlers` runs them in an executor. Every
call is bounded by the shared client in :mod:`bifrost_jupyter._apiclient`, so a
connected-but-silent server cannot pin a worker thread forever.

Upstream ``ApiException``\\s are translated to :class:`BifrostAPIError`, which
carries only the HTTP status and a fixed, safe message — never the upstream
response body, which could echo internal detail or the token.
"""

from __future__ import annotations

import os
from typing import Any

from bifrost_client import AccessApi, ApiException, ClustersApi, Configuration
from bifrost_client.models.cluster_view import ClusterView
from bifrost_client.models.create_cluster import CreateCluster

from . import _projects
from ._apiclient import DEFAULT_TIMEOUT_SECONDS, bounded_api_client
from ._credentials import (
    BifrostConfigError,
    CredentialError,
    CredentialSource,
    StaticCredential,
    reset_session,
    session_resolver,
)

API_URL_ENV_VAR = "BIFROST_API_URL"
#: Re-exported so existing callers/tests keep one import site for the dev PAT
#: variable; the full credential precedence lives in ``_credentials``.
TOKEN_ENV_VAR = "BIFROST_TOKEN"

__all__ = [
    "API_URL_ENV_VAR",
    "TOKEN_ENV_VAR",
    "BifrostAPIError",
    "BifrostClient",
    "BifrostConfigError",
    "CredentialError",
    "client_from_env",
    "reset_session",
]


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

#: Re-exported so callers can name the budget; the value, and the reasoning for
#: enforcing it at a chokepoint rather than per call site, live in
#: :mod:`bifrost_jupyter._apiclient`.
REQUEST_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS

#: A 401 that survived a credential refresh. Says what to do, because the design
#: is explicit that an expired credential must never reach the user as a mystery.
_STALE_CREDENTIAL_MESSAGE = (
    "unauthorized: bifrost rejected this session's credential even after refreshing it "
    "(see 'Authentication' in the bifrost-jupyter README)"
)

#: A 401 on a credential that cannot be refreshed (the dev ``bfr_`` PAT). Still
#: actionable — it just has a different remedy than the OIDC path's.
_REJECTED_CREDENTIAL_MESSAGE = (
    "unauthorized: bifrost rejected the configured credential — it may be expired or "
    "revoked (see 'Authentication' in the bifrost-jupyter README)"
)

#: A 403 on a cluster lifecycle call. Cluster create/stop/suspend/resume are Write
#: on TargetCluster, which only the operator (or admin) role grants — a Nebari
#: notebook user gets it from an IdP group mapped in Bifrost's auth config, either
#: globally ([roles].operator) or per project ([project_roles].operator). Without
#: that mapping the extension is unusable, so say so rather than "forbidden".
_LIFECYCLE_FORBIDDEN_MESSAGE = (
    "forbidden: your Bifrost identity may not manage clusters. Cluster lifecycle needs "
    "Write on 'cluster' — the 'operator' role. Ask a Bifrost admin to map your IdP group "
    "to operator (roles.operator or project_roles.operator in Bifrost's auth config)"
)


def _translate(exc: ApiException, forbidden: str | None = None) -> BifrostAPIError:
    status = exc.status if isinstance(exc.status, int) else 502
    # Never surface a raw upstream 5xx status to the browser as-is beyond 502.
    if status >= 500:
        return BifrostAPIError(502, "bifrost upstream error")
    if status == 403 and forbidden is not None:
        return BifrostAPIError(403, forbidden)
    return BifrostAPIError(status, _SAFE_MESSAGES.get(status, _DEFAULT_MESSAGE))


class BifrostClient:
    """A small facade exposing exactly the calls the panel needs.

    ``credentials`` is either a :class:`~bifrost_jupyter._credentials.CredentialSource`
    (the production path — refreshable, so a 401 is retried against a renewed
    token) or a plain token string, which is wrapped in a
    :class:`~bifrost_jupyter._credentials.StaticCredential`.
    """

    def __init__(
        self,
        api_url: str,
        credentials: CredentialSource | str,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        source: CredentialSource = (
            StaticCredential(credentials) if isinstance(credentials, str) else credentials
        )
        self._credentials = source
        self._timeout = timeout
        # The generated client reads ``access_token`` from this Configuration on
        # every request, so refreshing the credential is a field assignment —
        # no client rebuild, and calls already bound keep working.
        self._config = Configuration(host=api_url, access_token=source.get())
        # Bounded at the chokepoint, so this and every future endpoint wrapper
        # is timeout-safe without each call site remembering (see ._apiclient).
        client = bounded_api_client(self._config, timeout)
        self._clusters = ClustersApi(client)
        # Identity is read through the same bounded client, but as raw bytes:
        # the pinned SDK's IdentityResponse predates the `projects` field and
        # pydantic drops unknown fields, so a model round-trip would lose the
        # one thing the caller needs (see ._projects).
        self._access = AccessApi(client)

    def identity(self) -> dict | None:
        """The caller's identity as Bifrost reports it, or ``None`` if unreadable.

        Used to learn which projects the caller may start clusters in, so the
        panel never has to guess one. Raw rather than modelled — see the note
        in the constructor — and failure is soft: a client that cannot read its
        identity still starts clusters when the project is set another way, so
        this returns ``None`` rather than raising.
        """
        try:
            resp = self._call_access("identity_without_preload_content")
        except BifrostAPIError:
            raise
        except Exception:  # noqa: BLE001 - a malformed identity must not break start
            return None
        return _projects.parse_identity(getattr(resp, "data", None))

    def _call_access(self, name: str, *args: Any) -> Any:
        """``_call`` for ``AccessApi``, with the same 401 refresh."""
        return self._call(name, *args, _api=self._access)

    def _refresh_credential(self) -> bool:
        """Re-resolve the credential. ``False`` if this source cannot refresh."""
        if not self._credentials.refreshable:
            return False
        self._credentials.invalidate()
        try:
            self._config.access_token = self._credentials.get()
        except CredentialError as exc:
            raise BifrostAPIError(exc.status, exc.message) from None
        except BifrostConfigError:
            raise BifrostAPIError(401, _STALE_CREDENTIAL_MESSAGE) from None
        return True

    def _call(
        self, name: str, *args: Any, forbidden: str | None = None, _api: Any = None
    ) -> Any:
        """Invoke ``<api>.<name>``, refreshing the credential once on 401.

        ``_api`` defaults to the clusters API, which is what almost every call
        here wants; identity goes through the access API. The method is looked
        up by name on each attempt so the retry re-enters the same call site
        (and so tests can substitute one).
        """
        api = self._clusters if _api is None else _api
        for attempt in (0, 1):
            try:
                # No per-call timeout argument by design: the client this is
                # bound to supplies one for every request (see ._apiclient), so a
                # new endpoint wrapper cannot forget it.
                return getattr(api, name)(*args)
            except ApiException as exc:
                if exc.status == 401:
                    if attempt == 0 and self._refresh_credential():
                        continue
                    # Distinguish "we renewed it and bifrost still says no" from
                    # "this credential can't be renewed" — different remedies.
                    message = (
                        _STALE_CREDENTIAL_MESSAGE if attempt else _REJECTED_CREDENTIAL_MESSAGE
                    )
                    raise BifrostAPIError(401, message) from None
                raise _translate(exc, forbidden=forbidden) from None
        raise AssertionError("unreachable")  # pragma: no cover

    def create_cluster(self, body: CreateCluster) -> None:
        self._call("create_cluster", body, forbidden=_LIFECYCLE_FORBIDDEN_MESSAGE)

    def get_cluster(self, cluster_id: str) -> ClusterView:
        view: ClusterView = self._call("get_cluster", cluster_id)
        return view

    def list_clusters(self) -> list[ClusterView]:
        # Wraps Bifrost's project-scoped ``GET /api/v1/clusters`` (Read); the
        # server-side token scopes the result to the clusters the user may see.
        views: list[ClusterView] = self._call("list_clusters")
        return views

    def delete_cluster(self, cluster_id: str) -> None:
        # Wraps Bifrost's project-scoped ``DELETE /api/v1/clusters/{id}`` (Write);
        # stops (tears down) the cluster. 202-style async on the Bifrost side.
        self._call("delete_cluster", cluster_id, forbidden=_LIFECYCLE_FORBIDDEN_MESSAGE)

    def suspend_cluster(self, cluster_id: str) -> None:
        # Wraps Bifrost's project-scoped ``POST /api/v1/clusters/{id}/suspend``
        # (Write) — scales the cluster to zero while keeping its record.
        self._call("suspend_cluster", cluster_id, forbidden=_LIFECYCLE_FORBIDDEN_MESSAGE)

    def resume_cluster(self, cluster_id: str) -> None:
        # Wraps Bifrost's project-scoped ``POST /api/v1/clusters/{id}/resume``
        # (Write) — brings a suspended cluster back up.
        self._call("resume_cluster", cluster_id, forbidden=_LIFECYCLE_FORBIDDEN_MESSAGE)


def client_from_env() -> BifrostClient:
    """Construct a :class:`BifrostClient` from ``BIFROST_API_URL`` + the credential.

    Raises :class:`BifrostConfigError` when the extension is simply not
    configured (no API URL, no credential of any kind) — handlers render that as
    the friendly "not configured" state. A credential that exists but cannot be
    made usable (a stale OIDC token, an IdP that will not answer) is a
    :class:`BifrostAPIError` carrying an actionable message instead.
    """
    api_url = os.environ.get(API_URL_ENV_VAR)
    if not api_url:
        raise BifrostConfigError(f"{API_URL_ENV_VAR} is not set")
    credentials = session_resolver(api_url)
    try:
        # Resolve now — this is "session start": the exchange, and the PAT mint
        # when enabled, happen here rather than inside a later request.
        credentials.get()
    except CredentialError as exc:
        raise BifrostAPIError(exc.status, exc.message) from None
    return BifrostClient(api_url, credentials)
