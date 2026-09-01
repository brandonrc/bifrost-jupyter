"""Same-origin ``/bifrost/*`` server routes (design §3.2).

The browser panel calls only these routes, authenticated by Jupyter's own
XSRF/cookie; the Bifrost credential lives server-side and is attached here, never
in the browser. Responses never contain the token or upstream internal error
text — control-plane errors are mapped to ``{"error": <safe message>}`` with the
matching HTTP status.
"""

from __future__ import annotations

import json

import tornado
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join

from . import _address, _profiles
from .bifrost import BifrostAPIError, BifrostConfigError, client_from_env


class _BifrostHandler(APIHandler):
    """Shared error handling for Bifrost-backed routes."""

    def _fail(self, status: int, message: str) -> None:
        self.set_status(status)
        self.finish(json.dumps({"error": message}))

    def _client(self):
        # Raises BifrostConfigError if env is not configured; caught by callers.
        return client_from_env()


class ClustersHandler(_BifrostHandler):
    """``POST /bifrost/clusters`` — start a cluster from the ``small`` profile."""

    @tornado.web.authenticated
    def post(self) -> None:
        try:
            client = self._client()
        except BifrostConfigError:
            self._fail(500, "bifrost extension is not configured")
            return

        body = _profiles.build_create_cluster(_profiles.SMALL)
        try:
            client.create_cluster(body)
            view = client.get_cluster(body.id)
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return

        status = view.observed_state or view.desired or "pending"
        self.finish(json.dumps({"id": body.id, "status": status}))


class ClusterAddressHandler(_BifrostHandler):
    """``GET /bifrost/clusters/{id}/address`` — the gateway Jobs address + snippet."""

    @tornado.web.authenticated
    def get(self, cluster_id: str) -> None:
        try:
            client = self._client()
        except BifrostConfigError:
            self._fail(500, "bifrost extension is not configured")
            return

        try:
            host = client.gateway_host(cluster_id)
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return

        if host is None:
            self._fail(404, "cluster address not available")
            return

        self.finish(
            json.dumps(
                {
                    "jobs_address": _address.jobs_address(host),
                    "headers_hint": _address.headers_hint(),
                    "snippet": _address.connect_snippet(host),
                }
            )
        )


def setup_handlers(web_app) -> None:
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]

    clusters = url_path_join(base_url, "bifrost", "clusters")
    address = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "address")

    web_app.add_handlers(
        host_pattern,
        [
            (clusters, ClustersHandler),
            (address, ClusterAddressHandler),
        ],
    )
