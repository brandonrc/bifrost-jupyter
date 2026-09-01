"""Server route tests for the ``/bifrost/*`` routes.

Covers ``/bifrost/clusters`` (+ lifecycle), ``/clusters/{id}/address`` and the
Ray Jobs routes ``/clusters/{id}/jobs[/{job_id}]``.

The Bifrost client is faked for control-plane routes; these assert the
request/response contract and that the credential never appears in a response
body. The jobs routes talk to the cluster's Ray head service instead of Bifrost,
so there it is tornado's ``AsyncHTTPClient`` that is faked.
"""

import asyncio
import concurrent.futures
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest
from bifrost_client import ClustersApi
from tornado.httpclient import HTTPClientError, HTTPResponse

from bifrost_jupyter import _credentials, _jobs, bifrost, handlers
from bifrost_jupyter.bifrost import BifrostAPIError, BifrostConfigError
from bifrost_jupyter.tests import ROUTE_SSRF_IDS
from bifrost_jupyter.tests.test_credentials import make_jwt

TOKEN = "mob_supersecrettoken"


class FakeClient:
    def __init__(
        self,
        *,
        view=None,
        create_error=None,
        clusters=None,
        list_error=None,
        action_error=None,
    ):
        self._view = view
        self._create_error = create_error
        self._clusters = clusters or []
        self._list_error = list_error
        self._action_error = action_error
        self.created = None
        # (op_name, cluster_id) recorded for each lifecycle call.
        self.actions = []

    def create_cluster(self, body):
        if self._create_error:
            raise self._create_error
        self.created = body

    def get_cluster(self, cluster_id):
        return self._view

    def list_clusters(self):
        if self._list_error:
            raise self._list_error
        return self._clusters

    def delete_cluster(self, cluster_id):
        self.actions.append(("delete", cluster_id))
        if self._action_error:
            raise self._action_error

    def suspend_cluster(self, cluster_id):
        self.actions.append(("suspend", cluster_id))
        if self._action_error:
            raise self._action_error

    def resume_cluster(self, cluster_id):
        self.actions.append(("resume", cluster_id))
        if self._action_error:
            raise self._action_error


@pytest.fixture
def patch_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(handlers, "client_from_env", lambda: client)
        return client

    return _install


async def test_post_clusters_returns_id_and_status(jp_fetch, patch_client):
    view = SimpleNamespace(observed_state="running", desired="running")
    client = patch_client(FakeClient(view=view))

    resp = await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert resp.code == 200
    payload = json.loads(resp.body)

    assert payload["status"] == "running"
    assert payload["id"] == client.created.id
    # The token must never be echoed back to the browser.
    assert TOKEN not in resp.body.decode()


async def test_post_clusters_falls_back_to_desired_state(jp_fetch, patch_client):
    view = SimpleNamespace(observed_state=None, desired="running")
    patch_client(FakeClient(view=view))
    resp = await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert json.loads(resp.body)["status"] == "running"


async def test_post_clusters_maps_conflict(jp_fetch, patch_client):
    patch_client(FakeClient(create_error=BifrostAPIError(409, "conflict")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert exc.value.code == 409
    assert json.loads(exc.value.response.body)["error"] == "conflict"


async def test_post_clusters_unconfigured_is_clean_4xx(jp_fetch, monkeypatch):
    # A start when Bifrost is unconfigured is a user action, so it maps to a
    # clean 4xx (not a 5xx / error-level log), never an unhandled 500.
    def boom():
        raise BifrostConfigError("no token")

    monkeypatch.setattr(handlers, "client_from_env", boom)
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert exc.value.code == 409
    assert json.loads(exc.value.response.body)["error"] == "bifrost not configured"


async def test_post_clusters_missing_profile_is_rejected(jp_fetch, patch_client):
    # A body with no profile name must not silently default to a shape.
    patch_client(FakeClient(view=SimpleNamespace(observed_state="running", desired="running")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body="{}")
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "missing 'profile'"


@pytest.mark.parametrize("profile", [123, ["small"], {"name": "small"}, True])
async def test_post_clusters_non_string_profile_is_rejected(jp_fetch, patch_client, profile):
    # A non-string profile would hit a typed/unhashable lookup and 500; it must
    # be a clean 400 instead, with no cluster created.
    client = patch_client(FakeClient(view=SimpleNamespace(observed_state="running", desired="x")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body=json.dumps({"profile": profile}))
    assert exc.value.code == 400
    assert client.created is None


async def test_post_clusters_unknown_profile_is_rejected(jp_fetch, patch_client):
    # An unknown name is a clear 400 error, never a fallback to a default.
    client = patch_client(FakeClient(view=SimpleNamespace(observed_state="running", desired="x")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "enormous"}')
    assert exc.value.code == 400
    assert "unknown profile" in json.loads(exc.value.response.body)["error"]
    assert client.created is None  # no cluster was created


async def test_post_clusters_rejects_raw_spec_passthrough(jp_fetch, patch_client):
    # Extra body fields (an attempt to inject a raw spec) are ignored; only the
    # profile name selects the shape, which comes entirely from the allowlist.
    client = patch_client(FakeClient(view=SimpleNamespace(observed_state="running", desired="x")))
    body = json.dumps(
        {"profile": "small", "image": "evil:latest", "head_cpu": "64", "owner": "attacker"}
    )
    resp = await jp_fetch("bifrost", "clusters", method="POST", body=body)
    assert resp.code == 200
    created = client.created
    assert created.spec.image == "rayproject/ray:2.9.0"  # from the profile, not the body
    assert created.spec.head_cpu == "1"  # from the profile, not the body
    assert created.spec.owner is None  # never accepted from the client


async def test_get_clusters_returns_list_with_id_and_state(jp_fetch, patch_client):
    views = [
        SimpleNamespace(id="jl-small-aaa", observed_state="running", desired="running"),
        SimpleNamespace(id="jl-gpu-bbb", observed_state=None, desired="pending"),
    ]
    patch_client(FakeClient(clusters=views))

    resp = await jp_fetch("bifrost", "clusters", method="GET")
    assert resp.code == 200
    payload = json.loads(resp.body)

    assert payload["clusters"] == [
        {"id": "jl-small-aaa", "state": "running"},
        {"id": "jl-gpu-bbb", "state": "pending"},  # falls back to desired
    ]
    assert payload["configured"] is True
    # The token must never be echoed back to the browser.
    assert TOKEN not in resp.body.decode()


async def test_get_clusters_empty(jp_fetch, patch_client):
    patch_client(FakeClient(clusters=[]))
    resp = await jp_fetch("bifrost", "clusters", method="GET")
    assert resp.code == 200
    assert json.loads(resp.body)["clusters"] == []


async def test_get_clusters_maps_upstream_error(jp_fetch, patch_client):
    patch_client(FakeClient(list_error=BifrostAPIError(403, "forbidden")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="GET")
    assert exc.value.code == 403
    assert json.loads(exc.value.response.body)["error"] == "forbidden"


async def test_get_clusters_unconfigured_returns_200_configured_false(jp_fetch, monkeypatch):
    # A bare install (Bifrost unconfigured) is a normal state, not an error:
    # the load-time poll must get a clean 200 with configured:false and raise
    # nothing — no 5xx, no error-level ServerApp log.
    def boom():
        raise BifrostConfigError("no token")

    monkeypatch.setattr(handlers, "client_from_env", boom)
    resp = await jp_fetch("bifrost", "clusters", method="GET")
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload == {"clusters": [], "configured": False}


async def test_get_profiles_returns_safe_view(jp_fetch):
    resp = await jp_fetch("bifrost", "profiles")
    assert resp.code == 200
    body = resp.body.decode()
    payload = json.loads(body)

    names = {p["name"] for p in payload["profiles"]}
    assert {"small", "medium", "gpu"} <= names
    # The safe view carries the coarse shape but never a raw manifest surface.
    for p in payload["profiles"]:
        assert "description" in p and "head_cpu" in p
        assert "image" not in p
        assert "ray_version" not in p
    assert "rayproject/ray" not in body  # image string never leaks


async def test_get_profiles_unconfigured_returns_200(jp_fetch, monkeypatch):
    # Profiles are the static, config-driven allowlist and never touch Bifrost,
    # so the load-time poll returns 200 with the list even when Bifrost is
    # unconfigured — and raises nothing (would-be client construction blows up).
    def boom():
        raise BifrostConfigError("no token")

    monkeypatch.setattr(handlers, "client_from_env", boom)
    resp = await jp_fetch("bifrost", "profiles")
    assert resp.code == 200
    assert len(json.loads(resp.body)["profiles"]) >= 1


async def test_get_address_returns_in_cluster_snippet(jp_fetch):
    # Default namespace is "bifrost"; address is derived purely from id + namespace.
    resp = await jp_fetch("bifrost", "clusters", "cl-1", "address")
    assert resp.code == 200
    payload = json.loads(resp.body)

    assert payload["jobs_address"] == "http://cl-1-head-svc.bifrost.svc:8265"
    assert payload["ray_client_address"] == "ray://cl-1-head-svc.bifrost.svc:10001"
    assert "JobSubmissionClient" in payload["snippet"]
    # In-cluster path carries no token / auth header.
    assert TOKEN not in resp.body.decode()
    assert "Authorization" not in resp.body.decode()


async def test_delete_cluster_stops_and_returns_202(jp_fetch, patch_client):
    client = patch_client(FakeClient())
    resp = await jp_fetch("bifrost", "clusters", "jl-small-aaa", method="DELETE")
    assert resp.code == 202
    payload = json.loads(resp.body)
    assert payload == {"id": "jl-small-aaa", "status": "stopping"}
    # Mapped to delete_cluster with the path id, and nothing else.
    assert client.actions == [("delete", "jl-small-aaa")]
    assert TOKEN not in resp.body.decode()


async def test_delete_cluster_maps_not_found(jp_fetch, patch_client):
    patch_client(FakeClient(action_error=BifrostAPIError(404, "not found")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "ghost", method="DELETE")
    assert exc.value.code == 404
    assert json.loads(exc.value.response.body)["error"] == "not found"
    assert TOKEN not in exc.value.response.body.decode()


async def test_delete_cluster_unconfigured_is_clean_409(jp_fetch, monkeypatch):
    # Stop is a deliberate user action → clean 409 (not a 5xx), never a 500.
    def boom():
        raise BifrostConfigError("no token")

    monkeypatch.setattr(handlers, "client_from_env", boom)
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "jl-x", method="DELETE")
    assert exc.value.code == 409
    assert json.loads(exc.value.response.body)["error"] == "bifrost not configured"


async def test_suspend_cluster_maps_to_suspend_op(jp_fetch, patch_client):
    client = patch_client(FakeClient())
    resp = await jp_fetch("bifrost", "clusters", "jl-small-aaa", "suspend", method="POST", body="")
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload == {"id": "jl-small-aaa", "status": "suspending"}
    assert client.actions == [("suspend", "jl-small-aaa")]
    assert TOKEN not in resp.body.decode()


async def test_resume_cluster_maps_to_resume_op(jp_fetch, patch_client):
    client = patch_client(FakeClient())
    resp = await jp_fetch("bifrost", "clusters", "jl-small-aaa", "resume", method="POST", body="")
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload == {"id": "jl-small-aaa", "status": "resuming"}
    assert client.actions == [("resume", "jl-small-aaa")]


async def test_suspend_cluster_maps_conflict(jp_fetch, patch_client):
    patch_client(FakeClient(action_error=BifrostAPIError(409, "conflict")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "jl-x", "suspend", method="POST", body="")
    assert exc.value.code == 409
    assert json.loads(exc.value.response.body)["error"] == "conflict"


async def test_resume_cluster_unconfigured_is_clean_409(jp_fetch, monkeypatch):
    def boom():
        raise BifrostConfigError("no token")

    monkeypatch.setattr(handlers, "client_from_env", boom)
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "jl-x", "resume", method="POST", body="")
    assert exc.value.code == 409
    assert json.loads(exc.value.response.body)["error"] == "bifrost not configured"


async def test_lifecycle_rejects_unknown_action(jp_fetch, patch_client):
    # Only suspend|resume are routed to the lifecycle handler; any other verb on
    # /clusters/{id}/{action} is not a registered route → 404 (never a 500).
    client = patch_client(FakeClient())
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "jl-x", "obliterate", method="POST", body="")
    assert exc.value.code == 404
    assert client.actions == []


async def test_get_address_makes_no_backend_call(jp_fetch, monkeypatch):
    # The address path must NOT touch Bifrost (the registry endpoint is Admin-only).
    # Proven behaviorally: it succeeds even when constructing a Bifrost client would
    # blow up — i.e. it never calls client_from_env / any control-plane endpoint.
    def must_not_be_called():
        raise AssertionError("address path must not call the Bifrost control plane")

    monkeypatch.setattr(handlers, "client_from_env", must_not_be_called)
    resp = await jp_fetch("bifrost", "clusters", "cl-xyz", "address")
    assert resp.code == 200
    assert json.loads(resp.body)["jobs_address"] == "http://cl-xyz-head-svc.bifrost.svc:8265"


# --- Ray Jobs routes (requirement #11: env vars -> runtime_env.env_vars) -------
#
# These routes talk to the cluster's own Ray head service, not Bifrost, so the
# HTTP layer (tornado's AsyncHTTPClient inside ``_jobs``) is what gets faked.


class FakeRayServer:
    """Stands in for ``_jobs.AsyncHTTPClient``; records the requests it is sent."""

    def __init__(self, *, body=None, error=None, raises=None):
        self.requests = []
        self._body = body if body is not None else {}
        self._error = error
        self._raises = raises

    def __call__(self):  # AsyncHTTPClient() -> this recorder
        return self

    async def fetch(self, request):
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        if self._error is not None:
            raise self._error
        return HTTPResponse(request, 200, buffer=io.BytesIO(json.dumps(self._body).encode()))


@pytest.fixture
def ray_server(monkeypatch):
    def _install(**kwargs):
        server = FakeRayServer(**kwargs)
        monkeypatch.setattr(_jobs, "AsyncHTTPClient", server)
        return server

    return _install


async def test_post_job_puts_env_vars_in_runtime_env(jp_fetch, ray_server):
    server = ray_server(body={"job_id": "raysubmit_legacy", "submission_id": "raysubmit_abc"})

    body = json.dumps(
        {"entrypoint": "python train.py", "env_vars": {"HF_TOKEN": "t", "SEED": "7"}}
    )
    resp = await jp_fetch("bifrost", "clusters", "cl-1", "jobs", method="POST", body=body)
    assert resp.code == 200

    # The submission id Ray keys later calls on is ``submission_id``.
    assert json.loads(resp.body) == {
        "job_id": "raysubmit_abc",
        "submission_id": "raysubmit_abc",
    }

    # Exactly one call, to the in-cluster head service's Jobs API (trailing slash).
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.url == "http://cl-1-head-svc.bifrost.svc:8265/api/jobs/"
    assert request.method == "POST"

    # This is requirement #11: env vars land under runtime_env.env_vars.
    sent = json.loads(request.body)
    assert sent == {
        "entrypoint": "python train.py",
        "runtime_env": {"env_vars": {"HF_TOKEN": "t", "SEED": "7"}},
    }


async def test_post_job_without_env_vars_still_sends_runtime_env(jp_fetch, ray_server):
    server = ray_server(body={"submission_id": "raysubmit_abc"})
    await jp_fetch(
        "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
    )
    sent = json.loads(server.requests[0].body)
    assert sent == {"entrypoint": "echo hi", "runtime_env": {"env_vars": {}}}


async def test_post_job_carries_no_authorization_header(jp_fetch, ray_server):
    # The in-cluster path is gated by the per-owner NetworkPolicy, not a token:
    # the Bifrost credential must never be attached here.
    server = ray_server(body={"submission_id": "raysubmit_abc"})
    await jp_fetch(
        "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
    )
    headers = {k.lower() for k in server.requests[0].headers}
    assert "authorization" not in headers


async def test_post_job_falls_back_to_legacy_job_id(jp_fetch, ray_server):
    # Older Ray job servers answer with only the deprecated ``job_id`` field.
    ray_server(body={"job_id": "raysubmit_old"})
    resp = await jp_fetch(
        "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
    )
    assert json.loads(resp.body)["job_id"] == "raysubmit_old"


async def test_post_job_requires_an_entrypoint(jp_fetch, ray_server):
    server = ray_server(body={"submission_id": "x"})
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "jobs", method="POST", body="{}")
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "missing 'entrypoint'"
    assert server.requests == []  # nothing was submitted


@pytest.mark.parametrize(
    "env_vars",
    ["NOT_A_MAP", ["A=1"], {"A": 1}, {"A": None}, {"A": True}, {"A": ["1"]}, {"": "1"}],
)
async def test_post_job_rejects_bad_env_vars(jp_fetch, ray_server, env_vars):
    # Ray needs a flat str->str map; anything else is a clean 400, never a 500.
    server = ray_server(body={"submission_id": "x"})
    body = json.dumps({"entrypoint": "echo hi", "env_vars": env_vars})
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "jobs", method="POST", body=body)
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "invalid 'env_vars'"
    assert server.requests == []


async def test_post_job_rejects_invalid_body(jp_fetch, ray_server):
    ray_server(body={"submission_id": "x"})
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "jobs", method="POST", body="not json")
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "invalid request body"


async def test_post_job_unreachable_cluster_is_graceful(jp_fetch, ray_server):
    # A cluster that is not up yet (DNS/connect failure surfaces raw from tornado)
    # must map to a clean 502, never an unhandled 500.
    ray_server(raises=ConnectionRefusedError("nope"))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
        )
    assert exc.value.code == 502
    assert json.loads(exc.value.response.body)["error"] == "ray cluster unreachable"


async def test_post_job_connect_timeout_is_graceful(jp_fetch, ray_server):
    # tornado's synthetic 599 for a connect timeout maps the same way.
    ray_server(error=HTTPClientError(599, "Timeout"))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
        )
    assert exc.value.code == 502
    assert json.loads(exc.value.response.body)["error"] == "ray cluster unreachable"


async def test_post_job_upstream_5xx_maps_to_502(jp_fetch, ray_server):
    ray_server(error=HTTPClientError(500, "Internal Server Error"))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
        )
    assert exc.value.code == 502
    assert json.loads(exc.value.response.body)["error"] == "ray cluster error"


async def test_post_job_upstream_4xx_maps_to_safe_message(jp_fetch, ray_server):
    # The upstream body is discarded; only a fixed safe message is returned.
    ray_server(error=HTTPClientError(400, "Bad Request: internal detail leaks here"))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
        )
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "invalid job request"
    assert "internal detail" not in exc.value.response.body.decode()


async def test_post_job_works_when_bifrost_is_unconfigured(jp_fetch, ray_server, monkeypatch):
    # The jobs path never constructs a Bifrost client (the address is derived from
    # id + namespace), so an unconfigured install still submits — proven
    # behaviourally: it succeeds even when client_from_env would blow up.
    def must_not_be_called():
        raise AssertionError("the jobs path must not call the Bifrost control plane")

    monkeypatch.setattr(handlers, "client_from_env", must_not_be_called)
    ray_server(body={"submission_id": "raysubmit_abc"})
    resp = await jp_fetch(
        "bifrost", "clusters", "cl-1", "jobs", method="POST", body='{"entrypoint": "echo hi"}'
    )
    assert resp.code == 200
    assert json.loads(resp.body)["job_id"] == "raysubmit_abc"


async def test_get_job_status_maps_through(jp_fetch, ray_server):
    server = ray_server(
        body={
            "status": "RUNNING",
            "message": "Job is currently running.",
            "start_time": 1234,
            "end_time": None,
            # Fields the panel has no business seeing are dropped.
            "driver_agent_http_address": "http://10.0.0.7:52365",
            "driver_node_id": "node-abc",
            "runtime_env": {"env_vars": {"HF_TOKEN": "t"}},
        }
    )

    resp = await jp_fetch("bifrost", "clusters", "cl-1", "jobs", "raysubmit_abc", method="GET")
    assert resp.code == 200
    assert json.loads(resp.body) == {
        "job_id": "raysubmit_abc",
        "status": "RUNNING",
        "message": "Job is currently running.",
        "start_time": 1234,
        "end_time": None,
    }

    request = server.requests[0]
    assert request.url == "http://cl-1-head-svc.bifrost.svc:8265/api/jobs/raysubmit_abc"
    assert request.method == "GET"
    # The env var value must not be echoed back to the browser.
    assert "HF_TOKEN" not in resp.body.decode()


async def test_get_job_status_maps_not_found(jp_fetch, ray_server):
    ray_server(error=HTTPClientError(404, "Not Found"))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "jobs", "ghost", method="GET")
    assert exc.value.code == 404
    assert json.loads(exc.value.response.body)["error"] == "job not found"


async def test_get_job_status_unreachable_is_graceful(jp_fetch, ray_server):
    ray_server(raises=OSError("dns failure"))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "jobs", "raysubmit_abc", method="GET")
    assert exc.value.code == 502
    assert json.loads(exc.value.response.body)["error"] == "ray cluster unreachable"


async def test_get_job_status_non_json_body_is_graceful(jp_fetch, monkeypatch):
    # A cluster answering HTML (an ingress error page, say) must not 500.
    class Garbage:
        def __call__(self):
            return self

        async def fetch(self, request):
            return HTTPResponse(request, 200, buffer=io.BytesIO(b"<html>nope</html>"))

    monkeypatch.setattr(_jobs, "AsyncHTTPClient", Garbage())
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "jobs", "raysubmit_abc", method="GET")
    assert exc.value.code == 502
    assert json.loads(exc.value.response.body)["error"] == "invalid response from ray cluster"


# --- SSRF regression: cluster-id validation on every {id}-taking route ---------
#
# An id is a caller-controlled path segment interpolated straight into the
# head-service host and into the Bifrost control-plane URL. Unvalidated,
# ``evil.example:9999?`` made the *server* connect to an attacker-chosen host.
# Each route must answer a clean 400 and issue no upstream call at all.


@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_get_address_rejects_a_malformed_cluster_id(jp_fetch, cluster_id):
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", cluster_id, "address")
    assert exc.value.code == 400
    body = exc.value.response.body.decode()
    assert json.loads(body)["error"] == "invalid cluster id"
    # No address was derived, so nothing leaked back for the caller to read.
    assert "head-svc" not in body


@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_post_job_rejects_a_malformed_cluster_id(jp_fetch, ray_server, cluster_id):
    server = ray_server(body={"submission_id": "raysubmit_abc"})

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost",
            "clusters",
            cluster_id,
            "jobs",
            method="POST",
            body='{"entrypoint": "echo hi"}',
        )
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "invalid cluster id"
    assert server.requests == []


@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_get_job_status_rejects_a_malformed_cluster_id(jp_fetch, ray_server, cluster_id):
    server = ray_server(body={"status": "RUNNING"})

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", cluster_id, "jobs", "raysubmit_abc", method="GET")
    assert exc.value.code == 400
    assert server.requests == []


@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_delete_cluster_rejects_a_malformed_cluster_id(jp_fetch, patch_client, cluster_id):
    client = patch_client(FakeClient())

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", cluster_id, method="DELETE")
    assert exc.value.code == 400
    assert json.loads(exc.value.response.body)["error"] == "invalid cluster id"
    # The id also lands in the Bifrost control-plane URL — no call was made.
    assert client.actions == []


@pytest.mark.parametrize("action", ["suspend", "resume"])
@pytest.mark.parametrize("cluster_id", ROUTE_SSRF_IDS)
async def test_lifecycle_rejects_a_malformed_cluster_id(
    jp_fetch, patch_client, cluster_id, action
):
    client = patch_client(FakeClient())

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", cluster_id, action, method="POST", body="")
    assert exc.value.code == 400
    assert client.actions == []


async def test_reviewer_repro_is_rejected_on_the_jobs_route_too(jp_fetch, ray_server):
    # Same root cause as the dashboard finding, same payload, already-merged route.
    server = ray_server(body={"submission_id": "raysubmit_abc"})

    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost",
            "clusters",
            "evil.example:9999?",
            "jobs",
            method="POST",
            body='{"entrypoint": "echo hi"}',
        )
    assert exc.value.code == 400
    assert server.requests == []


# --- credential problems reach the panel as actionable errors (Task 9) -----
#
# A credential that exists but cannot be used is neither the bare-install
# "unconfigured" state nor an unhandled 500: it is a real status with a message
# the user can act on. Before Task 9 the handlers caught only BifrostConfigError
# around client construction, so this path was a 500.

CREDENTIAL_ERROR_MESSAGE = "the notebook's OIDC access token has expired and could not be refreshed"


@pytest.fixture
def stale_credential(monkeypatch):
    def boom():
        raise BifrostAPIError(401, CREDENTIAL_ERROR_MESSAGE)

    monkeypatch.setattr(handlers, "client_from_env", boom)


async def test_get_clusters_reports_a_credential_problem(jp_fetch, stale_credential):
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters")
    assert exc.value.code == 401
    assert json.loads(exc.value.response.body)["error"] == CREDENTIAL_ERROR_MESSAGE


async def test_post_clusters_reports_a_credential_problem(jp_fetch, stale_credential):
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert exc.value.code == 401
    assert json.loads(exc.value.response.body)["error"] == CREDENTIAL_ERROR_MESSAGE


async def test_stop_reports_a_credential_problem(jp_fetch, stale_credential):
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "jl-small-0123456789ab", method="DELETE")
    assert exc.value.code == 401
    assert json.loads(exc.value.response.body)["error"] == CREDENTIAL_ERROR_MESSAGE


@pytest.mark.parametrize("action", ["suspend", "resume"])
async def test_lifecycle_reports_a_credential_problem(jp_fetch, stale_credential, action):
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch(
            "bifrost", "clusters", "jl-small-0123456789ab", action, method="POST", body=""
        )
    assert exc.value.code == 401
    assert json.loads(exc.value.response.body)["error"] == CREDENTIAL_ERROR_MESSAGE


async def test_start_403_reaches_the_panel_with_the_operator_hint(jp_fetch, patch_client):
    """The T3 carry-forward: cluster create needs the operator role, so a 403
    must say that rather than leaving the user with a bare "forbidden"."""
    forbidden = BifrostAPIError(403, bifrost._LIFECYCLE_FORBIDDEN_MESSAGE)
    patch_client(FakeClient(create_error=forbidden))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert exc.value.code == 403
    assert "operator" in json.loads(exc.value.response.body)["error"]


async def test_oidc_credential_never_reaches_the_browser(jp_fetch, monkeypatch):
    """End-to-end through the real credential resolver: an auth_state-injected
    OIDC token is what authenticates the outbound call, and none of it appears
    in the response the panel receives."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv("BIFROST_API_URL", "https://bifrost.example")
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.TOKEN_ENV_VAR, TOKEN)

    seen = {}

    def fake_list(self, **_kwargs):
        seen["bearer"] = self.api_client.configuration.auth_settings()["bearer"]["value"]
        return []

    monkeypatch.setattr(ClustersApi, "list_clusters", fake_list)

    resp = await jp_fetch("bifrost", "clusters")

    assert resp.code == 200
    assert json.loads(resp.body) == {"clusters": [], "configured": True}
    # The OIDC token authenticated the outbound call ...
    assert seen["bearer"] == f"Bearer {oidc}"
    # ... and neither it nor the dev PAT is anywhere in the response.
    body = resp.body.decode()
    assert oidc not in body
    assert TOKEN not in body


# --- the IOLoop must stay responsive while Bifrost is slow (Task 11) --------
#
# jupyter-server is single-threaded. A blocking control-plane call made on the
# IOLoop thread stalls the WHOLE notebook server for its duration — kernel
# WebSocket traffic, file saves, the file browser, every other extension — so a
# panel poll against a slow Bifrost would freeze the user's entire Lab session.
# These assert the loop keeps serving while a Bifrost call is in flight.

#: Long enough that "the loop was blocked" and "the loop was free" cannot be
#: confused, short enough not to dominate the suite.
SLOW_CALL_SECONDS = 1.0


class SlowClient:
    """A Bifrost client whose calls block the calling thread, like a slow server."""

    def __init__(self, entered):
        self._entered = entered

    def list_clusters(self):
        self._entered.set()
        time.sleep(SLOW_CALL_SECONDS)
        return []

    def create_cluster(self, body):
        self._entered.set()
        time.sleep(SLOW_CALL_SECONDS)

    def get_cluster(self, cluster_id):
        return SimpleNamespace(observed_state="running", desired="running")

    def delete_cluster(self, cluster_id):
        self._entered.set()
        time.sleep(SLOW_CALL_SECONDS)

    def suspend_cluster(self, cluster_id):
        self._entered.set()
        time.sleep(SLOW_CALL_SECONDS)

    resume_cluster = suspend_cluster


async def _assert_loop_stays_responsive(jp_fetch, monkeypatch, slow_request):
    """Run ``slow_request`` and race a trivial request against it.

    The trivial request is issued only once the blocking call has actually
    started, and must finish first. If the blocking work runs on the IOLoop
    thread it cannot: the fast request is not even parsed until the slow one is
    done, so the completion order inverts and this fails (rather than hanging —
    the block is bounded).
    """
    entered = threading.Event()
    monkeypatch.setattr(handlers, "client_from_env", lambda: SlowClient(entered))
    order = []

    async def slow():
        await slow_request()
        order.append("slow")

    async def fast():
        while not entered.is_set():
            await asyncio.sleep(0.01)
        await jp_fetch("bifrost", "profiles")
        order.append("fast")

    await asyncio.gather(slow(), fast())
    assert order == ["fast", "slow"], (
        "the trivial request finished only after the slow Bifrost call — "
        "the IOLoop was blocked, so the whole notebook server was frozen"
    )


async def test_slow_list_does_not_block_the_loop(jp_fetch, monkeypatch):
    await _assert_loop_stays_responsive(
        jp_fetch, monkeypatch, lambda: jp_fetch("bifrost", "clusters")
    )


async def test_slow_start_does_not_block_the_loop(jp_fetch, monkeypatch):
    await _assert_loop_stays_responsive(
        jp_fetch,
        monkeypatch,
        lambda: jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}'),
    )


async def test_slow_stop_does_not_block_the_loop(jp_fetch, monkeypatch):
    await _assert_loop_stays_responsive(
        jp_fetch,
        monkeypatch,
        lambda: jp_fetch("bifrost", "clusters", "jl-small-0123456789ab", method="DELETE"),
    )


async def test_slow_suspend_does_not_block_the_loop(jp_fetch, monkeypatch):
    await _assert_loop_stays_responsive(
        jp_fetch,
        monkeypatch,
        lambda: jp_fetch(
            "bifrost", "clusters", "jl-small-0123456789ab", "suspend", method="POST", body=""
        ),
    )


async def test_slow_credential_resolution_does_not_block_the_loop(jp_fetch, monkeypatch):
    """Since Task 9 the *client construction* can do network I/O too — an OIDC
    refresh, an RFC 8693 exchange and a PAT mint, each with its own timeout. That
    path has to be off the loop as well, not just the Bifrost call after it."""
    entered = threading.Event()

    def slow_client_from_env():
        entered.set()
        time.sleep(SLOW_CALL_SECONDS)
        return FakeClient(clusters=[])

    monkeypatch.setattr(handlers, "client_from_env", slow_client_from_env)
    order = []

    async def slow():
        await jp_fetch("bifrost", "clusters")
        order.append("slow")

    async def fast():
        while not entered.is_set():
            await asyncio.sleep(0.01)
        await jp_fetch("bifrost", "profiles")
        order.append("fast")

    await asyncio.gather(slow(), fast())
    assert order == ["fast", "slow"]


# --- the blocking work runs on OUR pool, not the process-wide one ----------
#
# `run_in_executor(None, ...)` uses the default executor, which is per-event-loop
# and shared with all of jupyter-server: every other extension, and the server's
# own run_in_executor calls, draw from it. Filling it with slow Bifrost calls
# would starve them — the Task 11 harm moved one layer down, from the IOLoop to
# the pool, and harder to spot. So the extension owns a bounded, named pool.


async def test_blocking_work_runs_on_the_extensions_own_pool(jp_fetch, monkeypatch):
    """Identified by thread name, which is also what makes the pool legible in a
    stack dump or a `py-spy` attach."""
    seen = {}

    class ThreadNamingClient(FakeClient):
        def list_clusters(self):
            seen["thread"] = threading.current_thread().name
            return []

    monkeypatch.setattr(handlers, "client_from_env", lambda: ThreadNamingClient())

    await jp_fetch("bifrost", "clusters")

    assert seen["thread"].startswith(handlers.BLOCKING_POOL_PREFIX), (
        f"blocking work ran on {seen['thread']!r} — that is the process-wide "
        "default pool, shared with jupyter-server and every other extension"
    )


def test_the_pool_is_bounded_and_named():
    executor = handlers.blocking_executor()
    assert executor._max_workers == handlers.BLOCKING_POOL_SIZE
    assert executor is handlers.blocking_executor(), "the pool must be shared, not per-call"


async def test_the_pool_is_not_the_event_loops_default(jp_fetch, monkeypatch):
    """The distinction the fix turns on: had `_blocking` passed ``None``, the work
    would land on the loop's default executor instead."""
    loop = asyncio.get_running_loop()
    submitted = []

    class RecordingDefault(concurrent.futures.ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            # ``_blocking`` submits a functools.partial; unwrap to the real target.
            submitted.append(getattr(fn, "func", fn))
            return super().submit(fn, *args, **kwargs)

    loop.set_default_executor(RecordingDefault(max_workers=2))
    client = FakeClient(clusters=[])
    monkeypatch.setattr(handlers, "client_from_env", lambda: client)

    await jp_fetch("bifrost", "clusters")

    # asyncio itself legitimately uses the default pool (getaddrinfo for the test
    # client's own connection), so assert about *our* work specifically.
    ours = {handlers.client_from_env, client.list_clusters}
    leaked = [fn for fn in submitted if fn in ours]
    assert not leaked, (
        f"{leaked} ran on the loop's default executor — that pool belongs to the "
        "whole jupyter-server process, not to this extension"
    )
    assert submitted, "the recording executor was never consulted; the test proves nothing"
