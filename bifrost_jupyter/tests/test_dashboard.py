"""Ray-dashboard proxy: the ``/bifrost/clusters/{id}/dashboard*`` routes.

Like the Ray Jobs routes, this path never touches Bifrost — it talks to the
cluster's own head service, so it is tornado's ``AsyncHTTPClient`` that gets
faked here, not the Bifrost client.

The assertions worth keeping honest: the derived in-cluster address is what gets
fetched; nothing from the browser (cookies, ``Authorization``) is forwarded
upstream and only allowlisted headers come back; an unreachable cluster is a
clean 502 that does not echo the address; writes are not proxied; and the
response keeps jupyter-server's ``frame-ancestors 'self'`` CSP *without*
``default-src 'none'`` — which is what makes the in-Lab iframe render instead of
going blank.
"""

import io

import pytest
from tornado.httpclient import HTTPClientError, HTTPResponse
from tornado.httputil import HTTPHeaders

from bifrost_jupyter import _dashboard, handlers
from bifrost_jupyter.tests import ROUTE_SSRF_IDS

INDEX_HTML = b'<!DOCTYPE html><html><head><script src="./static/js/main.js"></script></head></html>'


class FakeRayDashboard:
    """Stands in for ``_dashboard.AsyncHTTPClient``; records what it is sent."""

    def __init__(self, *, status=200, body=INDEX_HTML, headers=None, raises=None):
        self.requests = []
        self._status = status
        self._body = body
        self._headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self._raises = raises

    def __call__(self):  # AsyncHTTPClient() -> this recorder
        return self

    async def fetch(self, request, raise_error=True):
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return HTTPResponse(
            request,
            self._status,
            headers=HTTPHeaders(self._headers),
            buffer=io.BytesIO(self._body),
        )


@pytest.fixture
def ray_dashboard(monkeypatch):
    def _install(**kwargs):
        server = FakeRayDashboard(**kwargs)
        monkeypatch.setattr(_dashboard, "AsyncHTTPClient", server)
        return server

    return _install


#
# Unit: URL construction and Location mapping.
#


def test_upstream_url_joins_without_letting_rest_switch_hosts():
    origin = "http://cl-1-head-svc.bifrost.svc:8265"
    # ``urljoin`` would read a protocol-relative rest as a new authority; plain
    # concatenation keeps it a (harmless) path on the pinned head service.
    assert (
        _dashboard.upstream_url(origin, "//evil.example/x", "")
        == "http://cl-1-head-svc.bifrost.svc:8265///evil.example/x"
    )


def test_upstream_url_requotes_the_decoded_path_and_appends_the_query():
    origin = "http://cl-1-head-svc.bifrost.svc:8265"
    # Tornado hands the capture over percent-decoded; a decoded "?" must not be
    # re-sent raw or it would inject a query string upstream.
    assert _dashboard.upstream_url(origin, "static/a?b", "") == (
        "http://cl-1-head-svc.bifrost.svc:8265/static/a%3Fb"
    )
    assert _dashboard.upstream_url(origin, "api/jobs/", "view=summary") == (
        "http://cl-1-head-svc.bifrost.svc:8265/api/jobs/?view=summary"
    )


def test_forwarded_location_maps_root_relative_into_the_prefix():
    prefix = "/bifrost/clusters/cl-1/dashboard/"
    assert _dashboard._forwarded_location("/api/x", prefix) == prefix + "api/x"
    # Already relative to the current document, which is inside the prefix.
    assert _dashboard._forwarded_location("nodes", prefix) == "nodes"
    # Absolute: points at the head service the browser cannot reach — dropped.
    assert _dashboard._forwarded_location("http://cl-1-head-svc.bifrost.svc:8265/", prefix) is None
    assert _dashboard._forwarded_location("//elsewhere.example/", prefix) is None


#
# Routes.
#


async def test_dashboard_root_redirects_to_trailing_slash(jp_fetch, jp_base_url):
    # Ray resolves its assets relative to the document URL, so the mount point
    # only works with the trailing slash.
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "dashboard", follow_redirects=False)
    assert exc.value.code == 302
    assert exc.value.response.headers["Location"] == (
        f"{jp_base_url}bifrost/clusters/cl-1/dashboard/"
    )


async def test_dashboard_proxies_index_from_the_derived_head_service(jp_fetch, ray_dashboard):
    server = ray_dashboard()

    resp = await jp_fetch("bifrost", "clusters", "cl-1", "dashboard/")
    assert resp.code == 200
    assert resp.body == INDEX_HTML
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.url == "http://cl-1-head-svc.bifrost.svc:8265/"
    assert request.method == "GET"
    # The NetworkPolicy is the gate: no credential of any kind goes upstream, and
    # the browser's own Jupyter cookie must never leak to the Ray head.
    assert "Authorization" not in request.headers
    assert "Cookie" not in request.headers


async def test_dashboard_proxies_asset_path_and_query(jp_fetch, ray_dashboard):
    server = ray_dashboard(body=b"console.log(1)", headers={"Content-Type": "text/javascript"})

    resp = await jp_fetch(
        "bifrost",
        "clusters",
        "cl-1",
        "dashboard",
        "static/js/main.abc.js",
        params={"v": "2"},
    )
    assert resp.code == 200
    assert resp.body == b"console.log(1)"
    assert resp.headers["Content-Type"] == "text/javascript"
    assert server.requests[0].url == (
        "http://cl-1-head-svc.bifrost.svc:8265/static/js/main.abc.js?v=2"
    )


async def test_dashboard_honors_the_configured_namespace(jp_fetch, ray_dashboard, jp_serverapp):
    jp_serverapp.web_app.settings["bifrost_cluster_namespace"] = "team-x"
    server = ray_dashboard()

    await jp_fetch("bifrost", "clusters", "cl-9", "dashboard/")
    assert server.requests[0].url == "http://cl-9-head-svc.team-x.svc:8265/"


async def test_dashboard_forwards_an_upstream_404(jp_fetch, ray_dashboard):
    ray_dashboard(status=404, body=b"not found", headers={"Content-Type": "text/plain"})

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "dashboard", "nope.js")
    assert exc.value.code == 404


async def test_dashboard_unreachable_cluster_is_a_clean_502(jp_fetch, ray_dashboard):
    # A stopped/suspended/starting cluster looks exactly like a refused connection.
    ray_dashboard(raises=OSError("Connection refused"))

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "dashboard/")
    assert exc.value.code == 502
    body = exc.value.response.body.decode()
    assert "ray cluster unreachable" in body
    # Nothing sensitive echoed: not the head-service address, not the namespace.
    assert "head-svc" not in body
    assert "8265" not in body


async def test_dashboard_does_not_call_the_bifrost_control_plane(
    jp_fetch, ray_dashboard, monkeypatch
):
    # The address is derived from (id, namespace); no Bifrost client is built, so
    # the dashboard keeps working on an install where Bifrost is unconfigured.
    ray_dashboard()

    def explode():
        raise AssertionError("the dashboard path must not call the Bifrost control plane")

    monkeypatch.setattr(handlers, "client_from_env", explode)
    resp = await jp_fetch("bifrost", "clusters", "cl-1", "dashboard/")
    assert resp.code == 200


async def test_dashboard_rejects_write_verbs(jp_fetch, ray_dashboard):
    server = ray_dashboard()

    # Ray's dashboard has write access to the cluster; proxying writes would need
    # an XSRF exemption, so only GET/HEAD are implemented.
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "cl-1", "dashboard", "api/jobs/", method="POST", body="{}"
        )
    assert exc.value.code == 405
    assert server.requests == []


async def test_dashboard_response_can_be_framed_same_origin(jp_fetch, ray_dashboard):
    ray_dashboard()

    resp = await jp_fetch("bifrost", "clusters", "cl-1", "dashboard/")
    csp = resp.headers["Content-Security-Policy"]
    # jupyter-server's own policy: an in-Lab iframe is same-origin, so it passes.
    assert "frame-ancestors 'self'" in csp
    # ``APIHandler`` would add this and leave the embedded dashboard blank; this
    # route is deliberately a plain ``JupyterHandler``.
    assert "default-src 'none'" not in csp
    assert "X-Frame-Options" not in resp.headers


async def test_dashboard_drops_upstream_cookies_and_framing_headers(jp_fetch, ray_dashboard):
    ray_dashboard(
        headers={
            "Content-Type": "text/html",
            "Set-Cookie": "ray_token=secret; Path=/",
            "X-Frame-Options": "DENY",
            "Content-Encoding": "gzip",
        }
    )

    resp = await jp_fetch("bifrost", "clusters", "cl-1", "dashboard/")
    # Allowlist, not denylist: a Ray cookie must not be planted on the Jupyter
    # origin, a stale Content-Encoding must not describe the decompressed body,
    # and an upstream X-Frame-Options must not break the in-Lab iframe.
    # (jupyter-server sets its own session Set-Cookie, so assert on the value:
    # Ray's cookie is what must not survive the hop.)
    assert "ray_token" not in "".join(resp.headers.get_list("Set-Cookie"))
    assert "X-Frame-Options" not in resp.headers
    assert "Content-Encoding" not in resp.headers


async def test_dashboard_maps_an_upstream_redirect_into_the_prefix(
    jp_fetch, jp_base_url, ray_dashboard
):
    ray_dashboard(status=302, body=b"", headers={"Location": "/index.html"})

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "cl-1", "dashboard", "old", follow_redirects=False
        )
    assert exc.value.code == 302
    assert exc.value.response.headers["Location"] == (
        f"{jp_base_url}bifrost/clusters/cl-1/dashboard/index.html"
    )


async def test_dashboard_head_proxies_without_a_body(jp_fetch, ray_dashboard):
    server = ray_dashboard()

    resp = await jp_fetch("bifrost", "clusters", "cl-1", "dashboard/", method="HEAD")
    assert resp.code == 200
    assert resp.body in (b"", None)
    assert server.requests[0].method == "HEAD"
    assert server.requests[0].url == "http://cl-1-head-svc.bifrost.svc:8265/"


#
# SSRF regression: an unvalidated cluster id used to pick the host the *server*
# connects to. Every payload must be a clean 400 with no upstream request.
#


@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_dashboard_rejects_a_malformed_cluster_id(jp_fetch, ray_dashboard, cluster_id):
    server = ray_dashboard()

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", cluster_id, "dashboard/")
    assert exc.value.code == 400
    assert "invalid cluster id" in exc.value.response.body.decode()
    # The load-bearing half: the server never opened a connection.
    assert server.requests == []


@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_dashboard_redirect_rejects_a_malformed_cluster_id(
    jp_fetch, ray_dashboard, cluster_id
):
    # The slash-less form must not 302 a hostile id into the proxy either.
    server = ray_dashboard()

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", cluster_id, "dashboard", follow_redirects=False)
    assert exc.value.code == 400
    assert server.requests == []


async def test_dashboard_reviewer_repro_reaches_no_attacker_host(jp_fetch, ray_dashboard):
    # The exact reported request: GET /bifrost/clusters/evil.example%3A9999%3F/dashboard/
    # used to fetch http://evil.example:9999?-head-svc.bifrost.svc:8265/ and reflect
    # the body back.
    server = ray_dashboard()

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "evil.example:9999?", "dashboard/")
    assert exc.value.code == 400
    assert server.requests == []
