"""Same-origin ``/bifrost/*`` server routes (design §3.2).

The browser panel calls only these routes, authenticated by Jupyter's own
XSRF/cookie; the Bifrost credential lives server-side and is attached here, never
in the browser. Responses never contain the token or upstream internal error
text — control-plane errors are mapped to ``{"error": <safe message>}`` with the
matching HTTP status.

Two families of route live here. Most are JSON APIs backed by Bifrost. The
exceptions are the in-cluster routes — ``/clusters/{id}/jobs*`` and
``/clusters/{id}/dashboard*`` — which talk straight to the cluster's own Ray head
service with no Bifrost credential at all; there the gate is the per-owner
NetworkPolicy *plus* the validated cluster id that pins the target (see
:class:`_ClusterIdMixin`). The dashboard route additionally serves HTML/JS rather
than JSON, so it is the one handler that is not an ``APIHandler`` (see
:class:`_ClusterDashboardBase`).

Every route taking an ``{id}`` path segment validates it first — control-plane
routes included, since the id also lands in the Bifrost URL.
"""

from __future__ import annotations

import functools
import http.client
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from urllib.parse import quote

import tornado
from jupyter_server.base.handlers import APIHandler, JupyterHandler
from jupyter_server.utils import url_path_join
from tornado.ioloop import IOLoop

from . import _address, _dashboard, _jobs, _profiles
from .bifrost import BifrostAPIError, BifrostConfigError, client_from_env
from .config import default_namespace


class _ClusterIdMixin:
    """The single guard every ``{id}``-taking route runs first.

    A cluster id is a path segment the caller fully controls, and it is
    interpolated into the head-service host (``<id>-head-svc.<ns>.svc``) and into
    the Bifrost control-plane URL. Unvalidated, an id like ``evil.example:9999?``
    restructures that URL and picks the host the *server* connects to — a blind
    SSRF from inside the cluster network, reachable by any external page riding
    the victim's ambient Jupyter cookie on a plain ``GET``.

    So validation lives in exactly one place (:func:`_address.validate_cluster_id`)
    and every route funnels through this mixin. Subclasses supply ``_fail`` so the
    400 is rendered in whatever the route's content type is.
    """

    #: Provided by the tornado handler this is mixed into.
    log: Any

    def _fail(self, status: int, message: str) -> None:
        raise NotImplementedError  # pragma: no cover - supplied by subclasses

    def _check_cluster_id(self, cluster_id: str) -> bool:
        """Answer a clean 400 and return ``False`` for a malformed id."""
        try:
            _address.validate_cluster_id(cluster_id)
        except _address.InvalidClusterIdError:
            # Logged at warning, not error, and without the id: a malformed id is
            # a rejected request, not a server fault, and echoing attacker input
            # into the ServerApp log is its own small hazard.
            self.log.warning("bifrost: rejected a malformed cluster id")
            self._fail(400, "invalid cluster id")
            return False
        return True


_T = TypeVar("_T")

#: Size of this extension's own thread pool.
#:
#: Small on purpose. The work is I/O-bound and already serialised twice over —
#: credential resolution holds a lock, and everything downstream talks to one
#: Bifrost — so more threads would buy queueing, not throughput. Four leaves
#: room for a stalled call plus the panel's status poll and a user action.
BLOCKING_POOL_SIZE = 4

#: Prefix for the pool's thread names, so a stack dump or a `py-spy` attach says
#: whose threads these are rather than showing anonymous workers.
BLOCKING_POOL_PREFIX = "bifrost-jupyter"

_EXECUTOR: ThreadPoolExecutor | None = None


def blocking_executor() -> ThreadPoolExecutor:
    """This extension's own bounded thread pool.

    Deliberately **not** the default executor that ``run_in_executor(None, …)``
    would use. That one is created per event loop and shared with the entire
    jupyter-server process: every other extension, and jupyter-server's own
    ``run_in_executor`` calls, draw from it. Filling it with our slow Bifrost
    calls would starve them — which is the same harm Task 11 set out to fix,
    moved one layer down from the IOLoop to the pool, and harder to see.

    A cold-cache credential resolve can hold a thread for tens of seconds
    (refresh, then exchange, then mint, each bounded), and the resolver's lock
    serialises concurrent resolves, so a handful of simultaneous panel requests
    genuinely can occupy several threads at once. Owning the pool keeps that
    cost inside this extension.

    **Saturation queues, it does not fail.** ``ThreadPoolExecutor``'s work queue
    is unbounded, so when all four threads are busy further requests wait their
    turn rather than being rejected. That is the right trade here — a queued
    panel poll is a slow panel, a rejected one is an error in the user's face —
    and it is bounded in practice because every outbound call carries a timeout
    (``bifrost.REQUEST_TIMEOUT_SECONDS``), so a thread cannot be held forever.
    The same bound caps how long interpreter shutdown can wait on these threads.

    Created lazily and shared process-wide: one pool per server, not per handler
    instance or per request.
    """
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=BLOCKING_POOL_SIZE, thread_name_prefix=BLOCKING_POOL_PREFIX
        )
    return _EXECUTOR


class _BifrostHandler(_ClusterIdMixin, APIHandler):
    """Shared error handling for Bifrost-backed routes.

    Every Bifrost control-plane call this class makes is **blocking** — the
    generated ``bifrost_client`` is urllib3-based, and since Task 9 constructing
    a client can additionally perform an OIDC refresh, an RFC 8693 exchange and
    a PAT mint. jupyter-server is single-threaded, so running any of that on the
    IOLoop thread stalls the *entire* notebook server for its duration: kernel
    WebSocket traffic, file saves, the file browser, every other extension. A
    panel poll against a slow Bifrost would freeze the user's whole Lab session.

    So the blocking work goes to a thread pool (:meth:`_blocking`) and the
    handlers are ``async``. That is one half of the fix; the other is the
    request timeout on every outbound call (:mod:`bifrost_jupyter._apiclient`),
    without which a connected-but-silent server would pin a pool thread forever
    and eventually starve the pool back into the same freeze.

    The pool is this extension's own (:func:`blocking_executor`), not the
    process-wide default, so a slow Bifrost cannot starve jupyter-server or
    another extension either.
    """

    def _fail(self, status: int, message: str) -> None:
        self.set_status(status)
        self.finish(json.dumps({"error": message}))

    async def _blocking(self, func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run ``func`` on this extension's own thread pool, awaiting the result.

        The pool is ours, not the process-wide default — see
        :func:`blocking_executor` for why that distinction matters.

        Exceptions propagate to the caller as if it had been called inline, so
        the ``BifrostAPIError``/``BifrostConfigError`` handling around each call
        site is unchanged.
        """
        return await IOLoop.current().run_in_executor(
            blocking_executor(), functools.partial(func, *args, **kwargs)
        )

    async def _client(self):
        # Blocking: resolves the credential (possibly refresh + exchange + mint)
        # and builds the client. Raises BifrostConfigError when the extension is
        # not configured, BifrostAPIError when a credential cannot be made
        # usable; both are caught by callers.
        return await self._blocking(client_from_env)

    async def _write_client_or_fail(self, action: str):
        """Return a configured client, or answer a clean 409 and return ``None``.

        A lifecycle action (stop/suspend/resume) is a deliberate user action, so —
        exactly like ``POST /clusters`` — an unconfigured extension maps to a clean
        409 logged at *warning* (never a 5xx / error-level ServerApp log), rather
        than the graceful ``configured:false`` 200 the load-time GET poll uses.

        A credential that *is* configured but cannot be made usable (an expired
        OIDC token with no refresh path, an unreachable IdP) is a different
        answer: :class:`BifrostAPIError` already carries the status and an
        actionable message, so it is relayed rather than flattened into
        "not configured".
        """
        try:
            return await self._client()
        except BifrostConfigError:
            self.log.warning("bifrost: %s requested but extension is not configured", action)
            self._fail(409, "bifrost not configured")
            return None
        except BifrostAPIError as exc:
            self.log.warning("bifrost: %s blocked by a credential problem", action)
            self._fail(exc.status, exc.message)
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


def _create_and_read(client, body):
    """Create a cluster and read it back — one unit of blocking work."""
    client.create_cluster(body)
    return client.get_cluster(body.id)


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
    async def get(self) -> None:
        try:
            client = await self._client()
        except BifrostConfigError:
            # "Never configured" is a normal, expected state for a bare install,
            # not an error. This route is polled on page load, so returning a
            # 5xx here would spam the ServerApp error log and fail the headless
            # Lab load check. Answer 200 with an explicit "unconfigured" shape
            # instead; the panel renders a friendly note and backs off polling.
            self.finish(json.dumps({"clusters": [], "configured": False}))
            return
        except BifrostAPIError as exc:
            # Configured, but the credential could not be made usable. That is a
            # real failure with an actionable message (an expired OIDC token, an
            # IdP that will not answer) — showing it is the whole point of the
            # refresh path, so it must not be swallowed as "unconfigured".
            self._fail(exc.status, exc.message)
            return

        try:
            views = await self._blocking(client.list_clusters)
        except BifrostAPIError as exc:
            self._fail(exc.status, exc.message)
            return

        clusters = [{"id": v.id, "state": _observed_state(v)} for v in views]
        self.finish(json.dumps({"clusters": clusters, "configured": True}))

    @tornado.web.authenticated
    async def post(self) -> None:
        # A start is a deliberate user action, not a page-load poll, so an
        # unconfigured extension is a clean 4xx (logged at warning, not error),
        # and a credential problem is relayed with its actionable message.
        client = await self._write_client_or_fail("start")
        if client is None:
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
            # Two round trips, both blocking; one hop to the pool covers both so
            # the create and its status read are not split across threads.
            view = await self._blocking(_create_and_read, client, body)
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
        if not self._check_cluster_id(cluster_id):
            return
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
    async def delete(self, cluster_id: str) -> None:
        if not self._check_cluster_id(cluster_id):
            return
        client = await self._write_client_or_fail("stop")
        if client is None:
            return
        try:
            await self._blocking(client.delete_cluster, cluster_id)
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
    async def post(self, cluster_id: str, action: str) -> None:
        if not self._check_cluster_id(cluster_id):
            return
        client = await self._write_client_or_fail(action)
        if client is None:
            return
        op = client.suspend_cluster if action == "suspend" else client.resume_cluster
        try:
            await self._blocking(op, cluster_id)
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
    per-owner NetworkPolicy admits it to :8265. The gate here is that NetworkPolicy
    **plus** the validated cluster id (see :class:`_ClusterIdMixin`) that pins the
    target to a head service in the configured namespace — **not** a bearer token.
    There is deliberately no ``Authorization`` header on this path and none should
    be added; the Bifrost credential is only for the control plane
    (create/list/delete/suspend/resume).

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
        if not self._check_cluster_id(cluster_id):
            return
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
        if not self._check_cluster_id(cluster_id):
            return
        try:
            view = await _jobs.get_job(self._jobs_address(cluster_id), job_id)
        except _jobs.RayJobsError as exc:
            self._fail(exc.status, exc.message)
            return
        self.finish(json.dumps(view))


class _ClusterDashboardBase(_ClusterIdMixin, JupyterHandler):
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

    def _fail(self, status: int, message: str) -> None:
        # Plain text, not JSON: the browser is the consumer on these routes.
        self.set_status(status)
        self.set_header("Content-Type", "text/plain; charset=UTF-8")
        self.finish(message)

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
        if not self._check_cluster_id(cluster_id):
            return
        self.redirect(self._dashboard_prefix(cluster_id))

    @tornado.web.authenticated
    def head(self, cluster_id: str) -> None:
        if not self._check_cluster_id(cluster_id):
            return
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
        # Before anything else: an unvalidated id picks the host this server
        # connects to. See ``_ClusterIdMixin``.
        if not self._check_cluster_id(cluster_id):
            return

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
            # not an error in this extension: answer plainly and never echo the
            # address.
            self._fail(exc.status, exc.message)
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
