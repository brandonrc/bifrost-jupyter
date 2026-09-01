"""Server route tests: /bifrost/clusters and /bifrost/clusters/{id}/address.

The Bifrost client is faked; these assert the request/response contract and that
the credential never appears in a response body.
"""

import json
from types import SimpleNamespace

import pytest
from tornado.httpclient import HTTPClientError

from bifrost_jupyter import handlers
from bifrost_jupyter.bifrost import BifrostAPIError, BifrostConfigError

TOKEN = "mob_supersecrettoken"


class FakeClient:
    def __init__(self, *, view=None, create_error=None, clusters=None, list_error=None):
        self._view = view
        self._create_error = create_error
        self._clusters = clusters or []
        self._list_error = list_error
        self.created = None

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
