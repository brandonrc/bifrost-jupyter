"""Same-origin ``/bifrost/*`` server routes (design §3.2).

The browser panel calls only these routes, authenticated by Jupyter's own
XSRF/cookie; the Bifrost credential lives server-side and is attached here, never
in the browser. Responses never contain the token or upstream internal error
text — control-plane errors are mapped to ``{"error": <safe message>}`` with the
matching HTTP status.

Two families of route live here. Most are JSON APIs backed by Bifrost. The
exceptions are the in-cluster routes — ``/clusters/{id}/jobs*`` and
``/clusters/{id}/dashboard*`` — which talk straight to the cluster's own Ray head
service with no Bifrost credential at all; the per-owner NetworkPolicy is the
gate there. The dashboard route additionally serves HTML/JS rather than JSON, so
it is the one handler that is not an ``APIHandler`` (see
:class:`_ClusterDashboardBase`).
"""

from __future__ import annotations

import http.client
import json
from urllib.parse import quote

import tornado
from jupyter_server.base.handlers import APIHandler, JupyterHandler
from jupyter_server.utils import url_path_join

from . import _address, _dashboard, _jobs, _profiles
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

    def _write_client_or_fail(self, action: str):
        """Return a configured client, or answer a clean 409 and return ``None``.

        A lifecycle action (stop/suspend/resume) is a deliberate user action, so —
        exactly like ``POST /clusters`` — an unconfigured extension maps to a clean
        409 logged at *warning* (never a 5xx / error-level ServerApp log), rather
        than the graceful ``configured:false`` 200 the load-time GET poll uses.
        """
        try:
            return self._client()
        except BifrostConfigError:
            self.log.warning("bifrost: %s requested but extension is not configured", action)
            self._fail(409, "bifrost not configured")
            return None

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
            # "Never configured" is a normal, expected state for a bare install,
            # not an error. This route is polled on page load, so returning a
            # 5xx here would spam the ServerApp error log and fail the headless
            # Lab load check. Answer 200 with an explicit "unconfigured" shape
            # instead; the panel renders a friendly note and backs off polling.
            self.finish(json.dumps({"clusters": [], "configured": False}))
            return

        try:
            views = client.list_clusters()
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return

        clusters = [{"id": v.id, "state": _observed_state(v)} for v in views]
        self.finish(json.dumps({"clusters": clusters, "configured": True}))

    @tornado.web.authenticated
    def post(self) -> None:
        try:
            client = self._client()
        except BifrostConfigError:
            # A start is a deliberate user action, not a page-load poll, so a
            # clean 4xx (logged at warning, not error) is the right answer when
            # Bifrost is not configured.
            self.log.warning("bifrost: start requested but extension is not configured")
            self._fail(409, "bifrost not configured")
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
        if not isinstance(name, str):
            # A non-string name would hit an unhashable/typed lookup downstream
            # and 500; reject it as a clean 4xx here.
            self._fail(400, "invalid 'profile'")
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


class ClusterHandler(_BifrostHandler):
    """``DELETE /bifrost/clusters/{id}`` — stop (tear down) a cluster.

    Maps to :meth:`BifrostClient.delete_cluster` (Bifrost ``DELETE
    /api/v1/clusters/{id}``). Stopping is destructive; the browser panel gates it
    behind a confirm, and the credential is attached server-side, never returned.
    """

    @tornado.web.authenticated
    def delete(self, cluster_id: str) -> None:
        client = self._write_client_or_fail("stop")
        if client is None:
            return
        try:
            client.delete_cluster(cluster_id)
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return
        # Bifrost tears down asynchronously (202); report the transitional state.
        self.set_status(202)
        self.finish(json.dumps({"id": cluster_id, "status": "stopping"}))


class ClusterLifecycleHandler(_BifrostHandler):
    """``POST /bifrost/clusters/{id}/{suspend|resume}`` — suspend/resume a cluster.

    The action is captured from the path and dispatched to
    :meth:`BifrostClient.suspend_cluster` / :meth:`~BifrostClient.resume_cluster`.
    Both are project-scoped Write ops that work for a normal token.
    """

    _TRANSITIONS = {"suspend": "suspending", "resume": "resuming"}

    @tornado.web.authenticated
    def post(self, cluster_id: str, action: str) -> None:
        client = self._write_client_or_fail(action)
        if client is None:
            return
        op = client.suspend_cluster if action == "suspend" else client.resume_cluster
        try:
            op(cluster_id)
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return
        self.finish(json.dumps({"id": cluster_id, "status": self._TRANSITIONS[action]}))


class _RayJobsHandler(_BifrostHandler):
    """Base for the two in-cluster Ray Jobs routes.

    NOTE: these routes do **not** talk to Bifrost. They talk straight to the
    cluster's own Ray head service (``<id>-head-svc.<ns>.svc:8265``), exactly
    like ``/clusters/{id}/address``. The jupyter-server extension runs inside the
    user's notebook pod, which carries the ``bifrost.dev/owner`` label, so the
    per-owner NetworkPolicy admits it to :8265 — the NetworkPolicy is the gate,
    **not** a bearer token. There is deliberately no ``Authorization`` header on
    this path and none should be added; the Bifrost credential is only for the
    control plane (create/list/delete/suspend/resume).

    A consequence worth stating: since no Bifrost client is constructed, these
    routes keep working on an install where Bifrost is unconfigured — the address
    is derived from the cluster id + configured namespace alone, so there is no
    ``BifrostConfigError`` to degrade from and never an unhandled 500.
    """

    def _jobs_address(self, cluster_id: str) -> str:
        return _address.jobs_address(cluster_id, self.settings["bifrost_cluster_namespace"])


class ClusterJobsHandler(_RayJobsHandler):
    """``POST /bifrost/clusters/{id}/jobs`` — submit a Ray job (requirement #11).

    Body: ``{"entrypoint": <str>, "env_vars": {<str>: <str>}}``. The env vars are
    the whole point of #11: ``ClusterSpec`` has no env field, so they attach to
    the *job* as ``runtime_env.env_vars`` (design §2, locked). Returns the Ray
    submission id as ``{"job_id": …, "submission_id": …}``.
    """

    @tornado.web.authenticated
    async def post(self, cluster_id: str) -> None:
        try:
            payload = json.loads(self.request.body or b"{}")
        except json.JSONDecodeError:
            self._fail(400, "invalid request body")
            return
        if not isinstance(payload, dict):
            self._fail(400, "invalid request body")
            return

        entrypoint = payload.get("entrypoint")
        if not entrypoint:
            self._fail(400, "missing 'entrypoint'")
            return
        if not isinstance(entrypoint, str):
            self._fail(400, "invalid 'entrypoint'")
            return

        try:
            env_vars = _jobs.clean_env_vars(payload.get("env_vars"))
        except ValueError as exc:
            self._fail(400, str(exc))
            return

        try:
            result = await _jobs.submit_job(self._jobs_address(cluster_id), entrypoint, env_vars)
        except _jobs.RayJobsError as exc:
            self._fail(exc.status, exc.message)
            return

        self.finish(json.dumps(result))


class ClusterJobHandler(_RayJobsHandler):
    """``GET /bifrost/clusters/{id}/jobs/{job_id}`` — one job's status.

    Maps Ray's ``GET /api/jobs/{submission_id}``, reduced to the allowlisted
    view (``status``/``message``/``start_time``/``end_time``) — not a passthrough
    of Ray's full ``JobDetails``.
    """

    @tornado.web.authenticated
    async def get(self, cluster_id: str, job_id: str) -> None:
        try:
            view = await _jobs.get_job(self._jobs_address(cluster_id), job_id)
        except _jobs.RayJobsError as exc:
            self._fail(exc.status, exc.message)
            return
        self.finish(json.dumps(view))


class _ClusterDashboardBase(JupyterHandler):
    """Base for the two Ray-dashboard routes (task 8).

    Deliberately :class:`~jupyter_server.base.handlers.JupyterHandler` and **not**
    :class:`~jupyter_server.base.handlers.APIHandler`: these routes serve the
    dashboard's own HTML/JS/CSS, and ``APIHandler`` both forces a JSON
    ``Content-Type`` and tightens the Content-Security-Policy to
    ``default-src 'none'``, which would leave an embedded dashboard blank.
    ``JupyterHandler``'s CSP is ``frame-ancestors 'self'``, which is exactly what
    lets the panel show the proxied dashboard in an in-Lab iframe.

    Auth is Jupyter's own (``@tornado.web.authenticated``); no Bifrost credential
    is involved and none is sent upstream — see :mod:`bifrost_jupyter._dashboard`.
    """

    def _dashboard_prefix(self, cluster_id: str) -> str:
        """The proxy mount path for this cluster, trailing slash included."""
        return (
            url_path_join(
                self.base_url, "bifrost", "clusters", quote(cluster_id, safe=""), "dashboard"
            )
            + "/"
        )


class ClusterDashboardRedirectHandler(_ClusterDashboardBase):
    """``GET /bifrost/clusters/{id}/dashboard`` → redirect to ``…/dashboard/``.

    Not cosmetic: Ray's dashboard resolves its assets and API calls *relative to
    the document URL*, so it only works when the mount URL ends in a slash. Ray's
    own reverse-proxy docs call this out and tell proxy operators to add exactly
    this redirect.
    """

    @tornado.web.authenticated
    def get(self, cluster_id: str) -> None:
        self.redirect(self._dashboard_prefix(cluster_id))

    @tornado.web.authenticated
    def head(self, cluster_id: str) -> None:
        self.redirect(self._dashboard_prefix(cluster_id))


class ClusterDashboardHandler(_ClusterDashboardBase):
    """``GET /bifrost/clusters/{id}/dashboard/*`` — the same-origin dashboard proxy.

    Streams the cluster's Ray dashboard (``<id>-head-svc.<ns>.svc:8265``) back
    under the Jupyter origin. Only ``GET``/``HEAD`` are implemented, so tornado
    answers every write verb with a 405 — see :mod:`bifrost_jupyter._dashboard`
    for why proxying writes would be a CSRF path into the cluster.
    """

    @tornado.web.authenticated
    async def get(self, cluster_id: str, rest: str) -> None:
        await self._proxy(cluster_id, rest, "GET")

    @tornado.web.authenticated
    async def head(self, cluster_id: str, rest: str) -> None:
        await self._proxy(cluster_id, rest, "HEAD")

    async def _proxy(self, cluster_id: str, rest: str, method: str) -> None:
        namespace = self.settings["bifrost_cluster_namespace"]
        origin = _address.dashboard_address(cluster_id, namespace)
        prefix = self._dashboard_prefix(cluster_id)

        try:
            proxied = await _dashboard.fetch(
                _dashboard.upstream_url(origin, rest, self.request.query),
                prefix,
                method=method,
            )
        except _dashboard.DashboardError as exc:
            # A cluster that is starting, stopped or suspended is unreachable,
            # not an error in this extension: answer plainly (the browser, not
            # the panel, is the consumer here) and never echo the address.
            self.set_status(exc.status)
            self.set_header("Content-Type", "text/plain; charset=UTF-8")
            self.finish(exc.message)
            return

        # ``set_status`` rejects a code tornado has no reason phrase for, which
        # would turn a weird upstream status into an unhandled 500.
        self.set_status(proxied.status if proxied.status in http.client.responses else 502)
        for name, value in proxied.headers.items():
            self.set_header(name, value)
        if method != "HEAD" and proxied.body:
            self.write(proxied.body)
        self.finish()


def setup_handlers(web_app, namespace: str | None = None, profiles=None) -> None:
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    web_app.settings["bifrost_cluster_namespace"] = namespace or default_namespace()
    web_app.settings["bifrost_profiles"] = profiles or _profiles.DEFAULT_PROFILES

    profiles_url = url_path_join(base_url, "bifrost", "profiles")
    clusters = url_path_join(base_url, "bifrost", "clusters")
    # Each pattern is anchored with ``$`` by tornado, and ``([^/]+)`` never spans a
    # slash, so ``/clusters/{id}``, ``/clusters/{id}/address``,
    # ``/clusters/{id}/{suspend|resume}``, ``/clusters/{id}/jobs``,
    # ``/clusters/{id}/jobs/{job_id}``, ``/clusters/{id}/dashboard`` and
    # ``/clusters/{id}/dashboard/<rest>`` are mutually exclusive —
    # order-independent. (``jobs``/``dashboard`` cannot collide with the
    # ``(suspend|resume)`` alternation either, and the two dashboard patterns are
    # split by the trailing slash the greedy ``(.*)`` sits behind.)
    cluster = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)")
    address = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "address")
    lifecycle = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", r"(suspend|resume)")
    jobs = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "jobs")
    job = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "jobs", r"([^/]+)")
    dashboard_root = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "dashboard")
    dashboard = url_path_join(base_url, "bifrost", "clusters", r"([^/]+)", "dashboard", r"(.*)")

    web_app.add_handlers(
        host_pattern,
        [
            (profiles_url, ProfilesHandler),
            (clusters, ClustersHandler),
            (address, ClusterAddressHandler),
            (lifecycle, ClusterLifecycleHandler),
            (job, ClusterJobHandler),
            (jobs, ClusterJobsHandler),
            (dashboard, ClusterDashboardHandler),
            (dashboard_root, ClusterDashboardRedirectHandler),
            (cluster, ClusterHandler),
        ],
    )
