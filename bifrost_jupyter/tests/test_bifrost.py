"""bifrost.py wrapper: credential attachment + safe error translation."""

import pytest
from bifrost_client import ApiException
from bifrost_client.models.registry_entry_view import RegistryEntryView

from bifrost_jupyter import bifrost
from bifrost_jupyter._profiles import build_create_cluster

API_URL = "https://bifrost.example"
TOKEN = "mob_supersecrettoken"


def test_credential_is_attached_to_outbound_config():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    config = client._clusters.api_client.configuration
    assert config.access_token == TOKEN
    # The client renders it as a Bearer Authorization header on the wire.
    assert config.auth_settings()["bearer"]["value"] == f"Bearer {TOKEN}"
    assert config.host == API_URL


def test_create_cluster_passes_body_through():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    captured = {}
    client._clusters.create_cluster = lambda body: captured.setdefault("body", body)

    body = build_create_cluster("small")
    client.create_cluster(body)
    assert captured["body"] is body


@pytest.mark.parametrize(
    "status,expected_status,expected_msg",
    [
        (401, 401, "unauthorized"),
        (403, 403, "forbidden"),
        (404, 404, "not found"),
        (409, 409, "conflict"),
        (422, 422, "invalid cluster specification"),
        (500, 502, "bifrost upstream error"),
        (503, 502, "bifrost upstream error"),
    ],
)
def test_error_translation_is_safe(status, expected_status, expected_msg):
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser(_cluster_id):
        raise ApiException(status=status, reason="upstream", body="SECRET internal stack trace")

    client._clusters.get_cluster = raiser

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.get_cluster("cl-1")

    err = exc_info.value
    assert err.status == expected_status
    assert err.message == expected_msg
    # The upstream body must never leak into the translated error.
    assert "SECRET" not in str(err)
    assert "stack trace" not in str(err)


def test_gateway_host_resolves_matching_entry():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    entries = [
        RegistryEntryView(
            id="other", hostname="other.gw", api_base_url="http://o:8265", token_set=True
        ),
        RegistryEntryView(
            id="cl-1", hostname="cl-1.gw.example", api_base_url="http://c:8265", token_set=True
        ),
    ]
    client._registry.list_registry = lambda: entries
    assert client.gateway_host("cl-1") == "cl-1.gw.example"


def test_gateway_host_returns_none_when_absent():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    client._registry.list_registry = lambda: []
    assert client.gateway_host("cl-1") is None


def test_client_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("BIFROST_API_URL", raising=False)
    monkeypatch.setenv("BIFROST_TOKEN", TOKEN)
    with pytest.raises(bifrost.BifrostConfigError):
        bifrost.client_from_env()


def test_client_from_env_requires_token(monkeypatch):
    monkeypatch.setenv("BIFROST_API_URL", API_URL)
    monkeypatch.delenv("BIFROST_TOKEN", raising=False)
    with pytest.raises(bifrost.BifrostConfigError):
        bifrost.client_from_env()


def test_client_from_env_builds_client(monkeypatch):
    monkeypatch.setenv("BIFROST_API_URL", API_URL)
    monkeypatch.setenv("BIFROST_TOKEN", TOKEN)
    client = bifrost.client_from_env()
    assert client._clusters.api_client.configuration.access_token == TOKEN
