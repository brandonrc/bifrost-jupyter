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
    def __init__(self, *, view=None, host=None, create_error=None, address_error=None):
        self._view = view
        self._host = host
        self._create_error = create_error
        self._address_error = address_error
        self.created = None

    def create_cluster(self, body):
        if self._create_error:
            raise self._create_error
        self.created = body

    def get_cluster(self, cluster_id):
        return self._view

    def gateway_host(self, cluster_id):
        if self._address_error:
            raise self._address_error
        return self._host


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


async def test_get_address_returns_snippet(jp_fetch, patch_client):
    patch_client(FakeClient(host="cl-1.gw.example"))
    resp = await jp_fetch("bifrost", "clusters", "cl-1", "address")
    assert resp.code == 200
    payload = json.loads(resp.body)

    assert payload["jobs_address"] == "https://cl-1.gw.example"
    assert payload["headers_hint"] == {"Authorization": "Bearer ${BIFROST_TOKEN}"}
    assert "JobSubmissionClient" in payload["snippet"]
    # No real token anywhere in the address payload.
    assert TOKEN not in resp.body.decode()


async def test_get_address_404_when_unregistered(jp_fetch, patch_client):
    patch_client(FakeClient(host=None))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-unknown", "address")
    assert exc.value.code == 404
    assert json.loads(exc.value.response.body)["error"] == "cluster address not available"


async def test_get_address_maps_forbidden(jp_fetch, patch_client):
    patch_client(FakeClient(address_error=BifrostAPIError(403, "forbidden")))
    with pytest.raises(HTTPClientError) as exc:
        await jp_fetch("bifrost", "clusters", "cl-1", "address")
    assert exc.value.code == 403
    assert json.loads(exc.value.response.body)["error"] == "forbidden"
