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
    def __init__(self, *, view=None, create_error=None):
        self._view = view
        self._create_error = create_error
        self.created = None

    def create_cluster(self, body):
        if self._create_error:
            raise self._create_error
        self.created = body

    def get_cluster(self, cluster_id):
        return self._view


@pytest.fixture
def patch_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(handlers, "client_from_env", lambda: client)
        return client

    return _install


async def test_post_clusters_returns_id_and_status(jp_fetch, patch_client):
    view = SimpleNamespace(observed_state="running", desired="running")
    client = patch_client(FakeClient(view=view))

    resp = await jp_fetch("bifrost", "clusters", method="POST", body="{}")
    assert resp.code == 200
    payload = json.loads(resp.body)

    assert payload["status"] == "running"
    assert payload["id"] == client.created.id
    # The token must never be echoed back to the browser.
    assert TOKEN not in resp.body.decode()


async def test_post_clusters_falls_back_to_desired_state(jp_fetch, patch_client):
    view = SimpleNamespace(observed_state=None, desired="running")
    patch_client(FakeClient(view=view))
    resp = await jp_fetch("bifrost", "clusters", method="POST", body="{}")
    assert json.loads(resp.body)["status"] == "running"


async def test_post_clusters_maps_conflict(jp_fetch, patch_client):
    patch_client(FakeClient(create_error=BifrostAPIError(409, "conflict")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body="{}")
    assert exc.value.code == 409
    assert json.loads(exc.value.response.body)["error"] == "conflict"


async def test_post_clusters_config_error(jp_fetch, monkeypatch):
    def boom():
        raise BifrostConfigError("no token")

    monkeypatch.setattr(handlers, "client_from_env", boom)
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", method="POST", body="{}")
    assert exc.value.code == 500
    assert json.loads(exc.value.response.body)["error"] == "bifrost extension is not configured"


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
