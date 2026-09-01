"""bifrost.py wrapper: credential attachment, refresh-on-401, safe errors."""

import time

import pytest
from bifrost_client import ApiException

from bifrost_jupyter import _credentials, bifrost
from bifrost_jupyter._profiles import build_create_cluster
from bifrost_jupyter.tests.test_credentials import make_jwt

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


def test_list_clusters_passes_through():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    views = [object(), object()]
    client._clusters.list_clusters = lambda: views
    assert client.list_clusters() is views


@pytest.mark.parametrize("op", ["delete_cluster", "suspend_cluster", "resume_cluster"])
def test_lifecycle_ops_pass_id_through(op):
    client = bifrost.BifrostClient(API_URL, TOKEN)
    captured = {}
    setattr(client._clusters, op, lambda cluster_id: captured.setdefault("id", cluster_id))

    getattr(client, op)("cl-42")
    assert captured["id"] == "cl-42"


@pytest.mark.parametrize("op", ["delete_cluster", "suspend_cluster", "resume_cluster"])
def test_lifecycle_ops_translate_error(op):
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser(_cluster_id):
        raise ApiException(status=409, reason="upstream", body="SECRET internal detail")

    setattr(client._clusters, op, raiser)
    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        getattr(client, op)("cl-1")
    err = exc_info.value
    assert err.status == 409
    assert err.message == "conflict"
    assert "SECRET" not in str(err)


def test_list_clusters_translates_error():
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser():
        raise ApiException(status=403, reason="upstream", body="SECRET internal detail")

    client._clusters.list_clusters = raiser
    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.list_clusters()
    err = exc_info.value
    assert err.status == 403
    assert err.message == "forbidden"
    assert "SECRET" not in str(err)


@pytest.mark.parametrize(
    "status,expected_status,expected_msg",
    [
        # A 401 is no longer the bare word "unauthorized": an expired credential
        # must tell the user what happened and what to do (design §4 / T9).
        (401, 401, bifrost._REJECTED_CREDENTIAL_MESSAGE),
        # ``get_cluster`` is a Read, so it keeps the generic 403; the lifecycle
        # calls carry the operator-role hint (asserted separately below).
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


# --- credential lifetime: a 401 renews, it does not surface as a mystery ---


class RotatingCredential:
    """A refreshable source that hands out the next token on each invalidation."""

    def __init__(self, tokens):
        self._tokens = list(tokens)
        self._current = self._tokens.pop(0)
        self.invalidations = 0

    @property
    def refreshable(self):
        return True

    def get(self):
        return self._current

    def invalidate(self):
        self.invalidations += 1
        if self._tokens:
            self._current = self._tokens.pop(0)


class UnrenewableCredential(RotatingCredential):
    """Refreshable in principle, but the OIDC token behind it is gone."""

    def get(self):
        if self.invalidations:
            raise _credentials.CredentialError(401, _credentials.EXPIRED_OIDC_MESSAGE)
        return super().get()


def test_401_refreshes_the_credential_and_retries_once():
    """The Task 9 requirement in one test: an expired credential triggers a
    refresh, and the retry goes out with the *new* token."""
    source = RotatingCredential(["stale-token", "fresh-token"])
    client = bifrost.BifrostClient(API_URL, source)
    seen = []

    def flaky():
        seen.append(client._config.access_token)
        if len(seen) == 1:
            raise ApiException(status=401, reason="upstream", body="SECRET token expired")
        return ["cluster"]

    client._clusters.list_clusters = flaky

    assert client.list_clusters() == ["cluster"]
    assert seen == ["stale-token", "fresh-token"]
    assert source.invalidations == 1


def test_refresh_is_attempted_at_most_once():
    """No retry storm against the control plane: one refresh, then report."""
    source = RotatingCredential(["one", "two", "three"])
    client = bifrost.BifrostClient(API_URL, source)
    attempts = []

    def always_401(_cluster_id):
        attempts.append(client._config.access_token)
        raise ApiException(status=401, reason="upstream", body="SECRET")

    client._clusters.get_cluster = always_401

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.get_cluster("cl-1")
    assert attempts == ["one", "two"]
    assert source.invalidations == 1
    assert exc_info.value.status == 401
    assert exc_info.value.message == bifrost._STALE_CREDENTIAL_MESSAGE
    assert "SECRET" not in str(exc_info.value)


def test_a_credential_that_cannot_be_renewed_reports_the_reason():
    """Refresh itself failed: the user sees why, not a bare 401."""
    client = bifrost.BifrostClient(API_URL, UnrenewableCredential(["stale"]))

    def always_401():
        raise ApiException(status=401, reason="upstream", body="SECRET")

    client._clusters.list_clusters = always_401

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.list_clusters()
    assert exc_info.value.status == 401
    assert exc_info.value.message == _credentials.EXPIRED_OIDC_MESSAGE
    assert "expired" in exc_info.value.message


def test_a_static_dev_pat_is_not_retried():
    """A pasted PAT has nothing to refresh to, so a 401 is reported at once."""
    client = bifrost.BifrostClient(API_URL, TOKEN)
    attempts = []

    def always_401():
        attempts.append(1)
        raise ApiException(status=401, reason="upstream", body="SECRET")

    client._clusters.list_clusters = always_401

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.list_clusters()
    assert attempts == [1]
    assert exc_info.value.message == bifrost._REJECTED_CREDENTIAL_MESSAGE


# --- 403: the role-mapping prerequisite, said out loud (T3 carry-forward) ---


@pytest.mark.parametrize("op", ["delete_cluster", "suspend_cluster", "resume_cluster"])
def test_lifecycle_403_names_the_operator_role(op):
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser(_cluster_id):
        raise ApiException(status=403, reason="upstream", body="SECRET internal detail")

    setattr(client._clusters, op, raiser)

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        getattr(client, op)("cl-1")
    message = exc_info.value.message
    assert exc_info.value.status == 403
    assert "operator" in message
    assert "SECRET" not in message


def test_create_403_names_the_operator_role():
    """Cluster CREATE needs Write on TargetCluster — developer/viewer get a 403,
    and without this message the panel would just say "forbidden"."""
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser(_body):
        raise ApiException(status=403, reason="upstream", body="SECRET internal detail")

    client._clusters.create_cluster = raiser

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.create_cluster(build_create_cluster("small"))
    assert exc_info.value.status == 403
    assert "operator" in exc_info.value.message
    assert "SECRET" not in exc_info.value.message


def test_read_403_stays_generic():
    """The operator hint belongs on lifecycle calls; a Read 403 means something
    else (wrong project), so it must not claim the wrong remedy."""
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser():
        raise ApiException(status=403, reason="upstream", body="SECRET")

    client._clusters.list_clusters = raiser

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.list_clusters()
    assert exc_info.value.message == "forbidden"


# --- client_from_env: production precedence -------------------------------


def test_client_from_env_prefers_the_oidc_token_over_the_dev_pat(monkeypatch):
    """The production swap: with an auth_state-injected token present, the dev
    PAT must not be what reaches Bifrost — the identity has to be the pod's."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv("BIFROST_API_URL", API_URL)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv("BIFROST_TOKEN", TOKEN)

    client = bifrost.client_from_env()

    config = client._clusters.api_client.configuration
    assert config.access_token == oidc
    assert config.auth_settings()["bearer"]["value"] == f"Bearer {oidc}"
    assert config.access_token != TOKEN


def test_client_from_env_works_with_only_an_oidc_token(monkeypatch):
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv("BIFROST_API_URL", API_URL)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)

    assert bifrost.client_from_env()._config.access_token == oidc


def test_client_from_env_maps_a_stale_oidc_token_to_an_actionable_401(monkeypatch):
    """Not a 500, not a silent "unconfigured": a credential problem the user can
    act on, with the status the panel renders as an error."""
    monkeypatch.setenv("BIFROST_API_URL", API_URL)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() - 10))

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        bifrost.client_from_env()
    assert exc_info.value.status == 401
    assert "expired" in exc_info.value.message


def test_client_from_env_reuses_the_session_credential(monkeypatch):
    """One resolution per session: two handler requests must not each re-do the
    exchange/mint."""
    monkeypatch.setenv("BIFROST_API_URL", API_URL)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() + 3600))

    first = bifrost.client_from_env()
    second = bifrost.client_from_env()
    assert first._credentials is second._credentials
