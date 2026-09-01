"""Same-origin reverse proxy for a cluster's Ray dashboard (task 8).

Ray serves the dashboard single-page app on the *same* head-service port as the
Jobs API (:8265), and the tier-2 per-owner NetworkPolicy (``kuberay.go``) already
admits the owner's notebook pod to that port. The jupyter-server extension runs
inside that pod, so it can reach the dashboard and re-serve it **same-origin**
under ``/bifrost/clusters/{id}/dashboard/`` — the dask-labextension shape, minus
the extra dependency.

**Why a hand-rolled proxy instead of ``jupyter-server-proxy``.** The usual reason
to reach for that package is SPA asset-path rewriting. Ray does not need it:

* Ray builds the dashboard with ``PUBLIC_URL="."`` (``dashboard/client/
  .env.production.local``, unchanged from Ray 2.9 through 2.58), so every static
  asset in ``index.html`` is referenced *relatively*.
* Its API calls go through ``formatUrl()`` in
  ``dashboard/client/src/service/requestHandlers.ts``, which strips a leading
  ``/`` precisely so requests are "relative to the path at which the dashboard is
  served … This works behind a reverse proxy".
* Routing is a ``HashRouter``, so client-side navigation lives in the URL
  fragment and never reaches the server — the document URL stays pinned at the
  mount point, which is what keeps relative resolution stable.

Ray's own docs say the dashboard "should work out-of-the-box when accessed via a
reverse proxy. API requests don't need to be proxied individually", with one
condition: the mount URL **must end in a trailing slash**, or the browser
resolves the relative paths one segment too high. Hence
:class:`~bifrost_jupyter.handlers.ClusterDashboardRedirectHandler`.

``jupyter-server-proxy`` would also have needed the operator to widen
``c.ServerProxy.host_allowlist`` to cover every ``*-head-svc.<ns>.svc`` — an
un-scoped ``/proxy/<host>:<port>/`` surface reachable by anything on the Jupyter
origin, not just this extension, and not tied to the caller's own clusters. More
dependency, more surface, less scoping, for rewriting Ray does not need.

**No Bifrost credential on this path**, exactly like :mod:`bifrost_jupyter._jobs`:
the address is derived from ``(cluster id, namespace)`` and reachability *is* the
authorization. Nothing is sent upstream either — in particular the browser's
``Cookie`` (Jupyter's session + XSRF cookie) and ``Authorization`` headers are
**not** forwarded to the Ray head, and only an allowlist of response headers
comes back.

**GET/HEAD only.** Ray's docs warn that "the Ray Dashboard provides read *and*
write access to the Ray Cluster". Proxying writes would mean exempting this route
from Jupyter's XSRF check (the dashboard's JS knows nothing about ``_xsrf``),
turning the extension into a CSRF path to cluster mutation. So this proxy is
deliberately read-only observability; the panel's own job routes remain the
supported write path.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPResponse

CONNECT_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 30.0

#: Verbs the proxy serves. See the module docstring: writes are not proxied.
ALLOWED_METHODS = ("GET", "HEAD")

#: The only upstream response headers passed back to the browser.
#:
#: An allowlist, not a denylist. ``Content-Encoding``/``Content-Length`` are
#: excluded because tornado transparently decompresses the body (forwarding them
#: would describe a body that no longer exists); ``Set-Cookie`` is excluded so a
#: Ray dashboard cookie can never be planted on the Jupyter origin; and anything
#: that could affect embedding (``X-Frame-Options``, ``Content-Security-Policy``)
#: is excluded so the response carries jupyter-server's own CSP —
#: ``frame-ancestors 'self'`` — which is what allows the in-Lab iframe.
FORWARDED_RESPONSE_HEADERS = ("Content-Type", "Cache-Control", "Content-Disposition")

_UNREACHABLE_MESSAGE = "ray cluster unreachable"


class DashboardError(RuntimeError):
    """A dashboard proxy failure reduced to a status + safe message.

    The upstream address is never part of ``message``: an unreachable cluster
    reports only that it is unreachable.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ProxiedResponse:
    """What the handler should write back: a status, safe headers, and a body."""

    status: int
    headers: dict[str, str]
    body: bytes


def upstream_url(origin: str, rest: str, query: str) -> str:
    """Build the upstream URL for a proxied sub-path.

    ``origin`` is :func:`bifrost_jupyter._address.dashboard_address` (no trailing
    slash); ``rest`` is the path *after* ``/dashboard/`` as tornado captured it
    (already percent-**decoded**, so it is re-quoted here — otherwise a ``%3F`` in
    an asset name would come back as a literal ``?`` and inject a query string);
    ``query`` is the raw query string, forwarded untouched.

    The join is deliberate string concatenation rather than ``urljoin``: a
    ``rest`` of ``//evil.example`` would make ``urljoin`` swap the *host*, while
    concatenation keeps it a (harmless) path on the pinned head service.
    """
    url = f"{origin}/{quote(rest, safe='/')}"
    if query:
        url = f"{url}?{query}"
    return url


def _forwarded_location(location: str, prefix: str) -> str | None:
    """Map an upstream ``Location`` into the proxy's prefix, or drop it.

    ``prefix`` is the proxy mount path, trailing slash included
    (``<base_url>/bifrost/clusters/{id}/dashboard/``).

    * An absolute URL points at the in-cluster head service, which the browser
      cannot reach and should not learn about — dropped.
    * A root-relative ``/api/x`` is relative to the *dashboard's* root, so it is
      re-rooted onto the prefix; forwarding it untouched would send the browser
      to ``/api/x`` on the Jupyter server instead.
    * Anything else is already relative to the current document, which is inside
      the prefix — forwarded as-is.
    """
    if location.startswith(("http://", "https://", "//")):
        return None
    if location.startswith("/"):
        return prefix + location.lstrip("/")
    return location


def _forwarded_headers(response: HTTPResponse, prefix: str) -> dict[str, str]:
    headers = {
        name: response.headers[name]
        for name in FORWARDED_RESPONSE_HEADERS
        if name in response.headers
    }
    # Redirects are handed to the browser rather than followed upstream (see
    # ``fetch``), so the ``Location`` has to be translated into our prefix.
    location = response.headers.get("Location")
    if location:
        mapped = _forwarded_location(location, prefix)
        if mapped is not None:
            headers["Location"] = mapped
    return headers


async def fetch(url: str, prefix: str, method: str = "GET") -> ProxiedResponse:
    """Fetch one dashboard URL and reduce it to a safe, forwardable response.

    ``prefix`` is the proxy mount path (trailing slash included) used to
    translate an upstream ``Location`` — see :func:`_forwarded_location`.

    Nothing from the incoming browser request is forwarded upstream: no cookies,
    no ``Authorization``, no ``Referer``. Non-2xx upstream statuses are passed
    through (the dashboard's own 404s are part of how the SPA works); a cluster
    that cannot be reached at all becomes a clean 502, never an unhandled 500.
    """
    request = HTTPRequest(
        url,
        method=method,
        headers={},
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        # Redirects are handed to the browser (see ``_forwarded_headers``) rather
        # than followed server-side, so the proxy can never be walked off the
        # pinned head service by an upstream ``Location``.
        follow_redirects=False,
    )

    client = AsyncHTTPClient()
    try:
        response = await client.fetch(request, raise_error=False)
    except Exception:
        # DNS failure, connection refused, timeout (tornado's synthetic 599),
        # TLS/socket errors: a cluster that is starting, stopped or suspended
        # looks exactly like this. Map it to a graceful 502.
        raise DashboardError(502, _UNREACHABLE_MESSAGE) from None

    return ProxiedResponse(
        status=response.code,
        headers=_forwarded_headers(response, prefix),
        body=response.body or b"",
    )
