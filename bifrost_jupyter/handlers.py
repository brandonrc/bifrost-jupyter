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
from .config import default_namespace


class _BifrostHandler(APIHandler):
    """Shared error handling for Bifrost-backed routes."""

    def _fail(self, status: int, message: str) -> None:
        self.set_status(status)
        self.finish(json.dumps({"error": message}))

    def _client(self):
        # Raises BifrostConfigError if env is not configured; caught by callers.
        return client_from_env()

    def _allowlist(self):
        # Resolved at extension load; defaults to the built-in set if unset.
        return self.settings.get("bifrost_profiles") or _profiles.DEFAULT_PROFILES


class ProfilesHandler(_BifrostHandler):
    """``GET /bifrost/profiles`` — the safe allowlist view (no manifest surface)."""

    @tornado.web.authenticated
    def get(self) -> None:
        views = _profiles.list_profiles(self._allowlist())
        self.finish(json.dumps({"profiles": [v.to_dict() for v in views]}))


def _observed_state(view) -> str:
    """The coarse, safe state for a cluster view: observed, else desired."""
    return view.observed_state or view.desired or "pending"


class ClustersHandler(_BifrostHandler):
    """``/bifrost/clusters`` — list clusters (GET) and start one (POST).

    ``POST`` body carries only a profile *name* (``{"profile": <name>}``); no
    field maps to a raw spec, so the allowlist is the only path to a
    ``ClusterSpec``. ``GET`` returns the project-scoped list/status view.
    """

    @tornado.web.authenticated
    def get(self) -> None:
        try:
            client = self._client()
        except BifrostConfigError:
            self._fail(500, "bifrost extension is not configured")
            return

        try:
            views = client.list_clusters()
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return

        clusters = [{"id": v.id, "state": _observed_state(v)} for v in views]
        self.finish(json.dumps({"clusters": clusters}))

    @tornado.web.authenticated
    def post(self) -> None:
        try:
            client = self._client()
        except BifrostConfigError:
            self._fail(500, "bifrost extension is not configured")
            return

        try:
            payload = json.loads(self.request.body or b"{}")
        except json.JSONDecodeError:
            self._fail(400, "invalid request body")
            return
        if not isinstance(payload, dict):
            self._fail(400, "invalid request body")
            return

        name = payload.get("profile")
        if not name:
            self._fail(400, "missing 'profile'")
            return

        try:
            body = _profiles.profile_to_spec(name, self._allowlist())
        except _profiles.UnknownProfileError as exc:
            self._fail(400, str(exc))
            return

        try:
            client.create_cluster(body)
            view = client.get_cluster(body.id)
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return

        self.finish(json.dumps({"id": body.id, "status": _observed_state(view)}))


class ClusterAddressHandler(_BifrostHandler):
    """``GET /bifrost/clusters/{id}/address`` — the in-cluster Jobs address + snippet.

    Derived purely from the cluster id and the configured namespace; makes no
    Bifrost call. The in-cluster path needs no auth header — the per-owner
    NetworkPolicy is the gate.
    """

    @tornado.web.authenticated
    def get(self, cluster_id: str) -> None:
        namespace = self.settings["bifrost_cluster_namespace"]
        self.finish(
            json.dumps(
                {
                    "jobs_address": _address.jobs_address(cluster_id, namespace),
                    "ray_client_address": _address.ray_client_address(cluster_id, namespace),
                    "snippet": _address.connect_snippet(cluster_id, namespace),
                }
            )
        )


def setup_handlers(web_app, namespace: str | None = None, profiles=None) -> None:
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    web_app.settings["bifrost_cluster_namespace"] = namespace or default_namespace()
    web_app.settings["bifrost_profiles"] = profiles or _profiles.DEFAULT_PROFILES

    profiles_url = url_path_join(base_url, "bifrost", "profiles")
    clusters = url_path_join(base_url, "bifrost", "clusters")
    address = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "address")

    web_app.add_handlers(
        host_pattern,
        [
            (profiles_url, ProfilesHandler),
            (clusters, ClustersHandler),
            (address, ClusterAddressHandler),
        ],
    )
