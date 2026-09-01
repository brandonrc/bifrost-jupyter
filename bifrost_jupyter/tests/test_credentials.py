"""Credential resolution (design §4, Task 9): precedence, exchange, lifetime.

Every test here asserts one of three things: the right credential wins, an
expiring credential is renewed rather than surfaced as a 401, or the token never
escapes into a message, a log line or a repr.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import time
import urllib.error
from types import SimpleNamespace

import pytest
from bifrost_client import ApiException

from bifrost_jupyter import _credentials

API_URL = "https://bifrost.example"
TOKEN_URL = "https://keycloak.example/realms/nebari/protocol/openid-connect/token"
DEV_PAT = "mob_devpatsupersecret"


def make_jwt(exp: float | None, sub: str = "8f14e45f-ceea-467a", username: str = "alice") -> str:
    """A structurally valid JWT. Never signed — nothing here verifies one."""
    claims: dict[str, object] = {"sub": sub, "preferred_username": username}
    if exp is not None:
        claims["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.c2ln"


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def idp(monkeypatch):
    """A fake IdP token endpoint recording the forms posted to it."""

    state = SimpleNamespace(requests=[], payload={"access_token": "exchanged-token"}, error=None)

    def fake_urlopen(request, timeout=None):
        form = dict(
            pair.split("=", 1)
            for pair in request.data.decode().split("&")
            if "=" in pair
        )
        state.requests.append(SimpleNamespace(url=request.full_url, form=form, timeout=timeout))
        if state.error is not None:
            raise state.error
        return FakeResponse(state.payload)

    monkeypatch.setattr(_credentials, "_urlopen", fake_urlopen)
    return state


@pytest.fixture
def fake_auth_api(monkeypatch):
    """A fake ``AuthApi`` recording the bearer the mint was made with."""

    state = SimpleNamespace(
        calls=[],
        response=SimpleNamespace(token="mob_minted", prefix="mob_min", expires_at=0),
        error=None,
    )

    class FakeAuthApi:
        def __init__(self, api_client):
            self._bearer = api_client.configuration.access_token
            self._host = api_client.configuration.host

        def create_token(self, request):
            state.calls.append(
                SimpleNamespace(
                    bearer=self._bearer,
                    host=self._host,
                    label=request.label,
                    expires_in_days=request.expires_in_days,
                )
            )
            if state.error is not None:
                raise state.error
            return state.response

    monkeypatch.setattr(_credentials, "AuthApi", FakeAuthApi)
    return state


# --- precedence -----------------------------------------------------------


def test_oidc_access_token_beats_the_dev_pat(monkeypatch):
    """Design §4: in production the OIDC identity must win, because it is the one
    that has to match the pod's owner label. A leftover dev PAT must not shadow
    it."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.TOKEN_ENV_VAR, DEV_PAT)

    credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == oidc
    assert credential.source == _credentials.SOURCE_OIDC
    assert credential.token != DEV_PAT


def test_dev_pat_is_used_when_no_oidc_token_is_injected(monkeypatch):
    monkeypatch.setenv(_credentials.TOKEN_ENV_VAR, DEV_PAT)
    credential = _credentials.CredentialResolver(API_URL).credential()
    assert credential.token == DEV_PAT
    assert credential.source == _credentials.SOURCE_DEV_PAT


def test_no_credential_at_all_is_a_config_error_naming_only_variables(monkeypatch):
    with pytest.raises(_credentials.BifrostConfigError) as exc_info:
        _credentials.CredentialResolver(API_URL).credential()
    message = str(exc_info.value)
    assert _credentials.OIDC_TOKEN_ENV_VAR in message
    assert _credentials.TOKEN_ENV_VAR in message


def test_token_file_beats_the_environment_and_is_re_read(monkeypatch, tmp_path):
    """The file form exists so a rotated token is picked up without a respawn: a
    spawn-time environment variable is frozen for the life of the pod."""
    path = tmp_path / "access_token"
    path.write_text(make_jwt(time.time() + 3600, username="from-file") + "\n")
    monkeypatch.setenv(_credentials.OIDC_TOKEN_FILE_ENV_VAR, str(path))
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() + 3600))

    resolver = _credentials.CredentialResolver(API_URL)
    first = resolver.credential().token
    assert first == path.read_text().strip()

    rotated = make_jwt(time.time() + 7200, username="rotated")
    path.write_text(rotated)
    resolver.invalidate()
    assert resolver.credential().token == rotated


def test_hub_access_token_env_var_is_accepted(monkeypatch):
    """``ACCESS_TOKEN`` is the name JupyterHub's own auth_state_hook examples use."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.HUB_OIDC_TOKEN_ENV_VAR, oidc)
    assert _credentials.CredentialResolver(API_URL).credential().token == oidc


def test_unreadable_token_file_falls_back_to_the_environment(monkeypatch, tmp_path, caplog):
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_FILE_ENV_VAR, str(tmp_path / "missing"))
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    with caplog.at_level(logging.WARNING, logger="bifrost_jupyter"):
        assert _credentials.CredentialResolver(API_URL).credential().token == oidc
    assert "unreadable" in caplog.text
    assert oidc not in caplog.text


# --- RFC 8693 exchange ----------------------------------------------------


def test_exchange_is_used_when_an_audience_is_configured(monkeypatch, idp):
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_URL_ENV_VAR, TOKEN_URL)
    monkeypatch.setenv(_credentials.OIDC_CLIENT_ID_ENV_VAR, "bifrost-jupyter")
    monkeypatch.setenv(_credentials.OIDC_CLIENT_SECRET_ENV_VAR, "s3cret")
    monkeypatch.setenv(_credentials.OIDC_AUDIENCE_ENV_VAR, "bifrost")
    idp.payload = {"access_token": "aud-bifrost-token", "expires_in": 300}

    credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == "aud-bifrost-token"
    assert credential.source == _credentials.SOURCE_OIDC_EXCHANGED
    # Expiry comes from the exchange response, so the next call knows to renew.
    assert credential.expires_at is not None
    assert 250 < credential.expires_at - time.time() <= 300

    (request,) = idp.requests
    assert request.url == TOKEN_URL
    assert request.form["grant_type"] == _credentials.GRANT_TYPE_TOKEN_EXCHANGE.replace(":", "%3A")
    assert request.form["subject_token"] == oidc
    assert request.form["audience"] == "bifrost"
    assert request.form["client_id"] == "bifrost-jupyter"


def test_no_exchange_without_an_audience(monkeypatch, idp):
    """The exchange is for audience mismatch only; a matching audience must not
    add a per-session round trip to the IdP."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_URL_ENV_VAR, TOKEN_URL)
    monkeypatch.setenv(_credentials.OIDC_CLIENT_ID_ENV_VAR, "bifrost-jupyter")

    assert _credentials.CredentialResolver(API_URL).credential().token == oidc
    assert idp.requests == []


def test_exchange_refuses_a_cleartext_token_endpoint(monkeypatch, idp):
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() + 3600))
    monkeypatch.setenv(_credentials.OIDC_TOKEN_URL_ENV_VAR, "http://keycloak.example/token")
    monkeypatch.setenv(_credentials.OIDC_CLIENT_ID_ENV_VAR, "bifrost-jupyter")
    monkeypatch.setenv(_credentials.OIDC_AUDIENCE_ENV_VAR, "bifrost")

    with pytest.raises(_credentials.CredentialError) as exc_info:
        _credentials.CredentialResolver(API_URL).credential()
    assert _credentials.OIDC_TOKEN_URL_ENV_VAR in exc_info.value.message
    # Nothing was sent: the subject token would have crossed the wire in clear.
    assert idp.requests == []


def test_exchange_failure_never_echoes_the_subject_token(monkeypatch, idp):
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_URL_ENV_VAR, TOKEN_URL)
    monkeypatch.setenv(_credentials.OIDC_CLIENT_ID_ENV_VAR, "bifrost-jupyter")
    monkeypatch.setenv(_credentials.OIDC_AUDIENCE_ENV_VAR, "bifrost")
    # A real IdP error body echoes the request parameters back at you.
    idp.error = urllib.error.HTTPError(
        TOKEN_URL, 400, "Bad Request", {}, None  # type: ignore[arg-type]
    )

    with pytest.raises(_credentials.CredentialError) as exc_info:
        _credentials.CredentialResolver(API_URL).credential()
    assert exc_info.value.status == 502
    assert oidc not in exc_info.value.message
    assert oidc not in str(exc_info.value)


def test_exchange_without_an_access_token_in_the_body_is_an_error(monkeypatch, idp):
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() + 3600))
    monkeypatch.setenv(_credentials.OIDC_TOKEN_URL_ENV_VAR, TOKEN_URL)
    monkeypatch.setenv(_credentials.OIDC_CLIENT_ID_ENV_VAR, "bifrost-jupyter")
    monkeypatch.setenv(_credentials.OIDC_AUDIENCE_ENV_VAR, "bifrost")
    idp.payload = {"error": "invalid_grant"}

    with pytest.raises(_credentials.CredentialError):
        _credentials.CredentialResolver(API_URL).credential()


# --- lifetime: expiry triggers a refresh, never a silent 401 ---------------


def test_expired_access_token_is_refreshed_at_the_idp(monkeypatch, idp):
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() - 10))
    monkeypatch.setenv(_credentials.OIDC_REFRESH_TOKEN_ENV_VAR, "refresh-me")
    monkeypatch.setenv(_credentials.OIDC_TOKEN_URL_ENV_VAR, TOKEN_URL)
    monkeypatch.setenv(_credentials.OIDC_CLIENT_ID_ENV_VAR, "bifrost-jupyter")
    idp.payload = {"access_token": "fresh-token", "expires_in": 600}

    credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == "fresh-token"
    assert credential.source == _credentials.SOURCE_OIDC_REFRESHED
    (request,) = idp.requests
    assert request.form["grant_type"] == "refresh_token"
    assert request.form["refresh_token"] == "refresh-me"


def test_expired_access_token_without_a_refresh_path_is_an_actionable_401(monkeypatch):
    """The failure the design forbids surfacing as a mystery: say what happened
    and what to do, with a status the panel renders as an error."""
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() - 10))

    with pytest.raises(_credentials.CredentialError) as exc_info:
        _credentials.CredentialResolver(API_URL).credential()
    assert exc_info.value.status == 401
    assert "expired" in exc_info.value.message
    assert "restart" in exc_info.value.message


def test_a_credential_expiring_mid_session_is_re_resolved(monkeypatch, tmp_path, idp):
    """Cached until it expires, then resolved again — the long-session case."""
    path = tmp_path / "access_token"
    path.write_text(make_jwt(time.time() + 3600))
    monkeypatch.setenv(_credentials.OIDC_TOKEN_FILE_ENV_VAR, str(path))

    resolver = _credentials.CredentialResolver(API_URL)
    first = resolver.credential()
    assert resolver.credential() is first  # cached: no re-read while valid

    # Now the cached credential is past its expiry and the hub has rotated the
    # file underneath us.
    expired = _credentials.Credential(first.token, first.source, time.time() - 1)
    resolver._cached = expired
    rotated = make_jwt(time.time() + 3600, username="rotated")
    path.write_text(rotated)

    assert resolver.credential().token == rotated


def test_expiry_skew_renews_before_the_deadline():
    credential = _credentials.Credential("t", "oidc", time.time() + 5)
    assert credential.is_expired(), "a credential expiring in 5s must be renewed now"
    assert not _credentials.Credential("t", "oidc", time.time() + 3600).is_expired()
    assert not _credentials.Credential("t", "dev-pat", None).is_expired()


def test_jwt_expiry_tolerates_non_jwt_and_junk():
    assert _credentials.jwt_expiry(DEV_PAT) is None
    assert _credentials.jwt_expiry("a.b.c") is None
    assert _credentials.jwt_expiry(make_jwt(None)) is None
    assert _credentials.jwt_expiry(make_jwt(1234567890)) == 1234567890.0


# --- session PAT mint (opt-in) --------------------------------------------


def test_pat_mint_is_off_by_default(monkeypatch, fake_auth_api):
    """Verified against the Bifrost source: on an OIDC deployment the mint either
    404s (no local auth), 500s (no local user for the OIDC sub), or — worst —
    succeeds and hands back a PAT whose owner is the sub instead of
    preferred_username, silently breaking the per-owner NetworkPolicy match. So
    it must not happen unless an operator asks for it."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)

    credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == oidc
    assert fake_auth_api.calls == []


def test_pat_mint_when_enabled_uses_the_oidc_bearer(monkeypatch, fake_auth_api):
    """The mint must be made *as the OIDC identity* — that is what ties the PAT
    to the same subject, and it is why the bearer, not some other credential, is
    what goes on the mint call."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.MINT_PAT_ENV_VAR, "1")
    expires_at = time.time() + 86_400
    fake_auth_api.response = SimpleNamespace(
        token="mob_minted", prefix="mob_min", expires_at=expires_at
    )

    credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == "mob_minted"
    assert credential.source == _credentials.SOURCE_MINTED_PAT
    assert credential.expires_at == expires_at
    (call,) = fake_auth_api.calls
    assert call.bearer == oidc
    assert call.host == API_URL
    assert call.expires_in_days == 1


def test_pat_ttl_days_is_clamped_to_the_server_maximum(monkeypatch, fake_auth_api):
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() + 3600))
    monkeypatch.setenv(_credentials.MINT_PAT_ENV_VAR, "true")
    monkeypatch.setenv(_credentials.PAT_TTL_DAYS_ENV_VAR, "9999")

    _credentials.CredentialResolver(API_URL).credential()
    assert fake_auth_api.calls[0].expires_in_days == 90


def test_unavailable_pat_mint_degrades_to_the_oidc_token(monkeypatch, fake_auth_api, caplog):
    """``POST /api/v1/auth/tokens`` 404s on an OIDC-only Bifrost (it is gated on
    local auth). That must not end the session."""
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.MINT_PAT_ENV_VAR, "1")
    fake_auth_api.error = ApiException(
        status=404, reason="Not Found", body='{"message":"local auth is not enabled"}'
    )

    with caplog.at_level(logging.WARNING, logger="bifrost_jupyter"):
        credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == oidc
    assert credential.source == _credentials.SOURCE_OIDC
    assert "404" in caplog.text
    # Neither the token nor the upstream body is relayed into the log.
    assert oidc not in caplog.text
    assert "local auth is not enabled" not in caplog.text


def test_pat_mint_transport_failure_degrades_to_the_oidc_token(monkeypatch, fake_auth_api, caplog):
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    monkeypatch.setenv(_credentials.MINT_PAT_ENV_VAR, "1")
    fake_auth_api.error = OSError("connection refused")

    with caplog.at_level(logging.WARNING, logger="bifrost_jupyter"):
        credential = _credentials.CredentialResolver(API_URL).credential()

    assert credential.token == oidc
    assert oidc not in caplog.text


# --- the token never escapes ----------------------------------------------


def test_credential_cannot_be_dumped_field_by_field():
    """Redaction covers ``repr``/``str``; the generic *structural* dumps walk the
    fields instead and would hand back the plaintext. ``Credential`` is therefore
    not a dataclass and has no instance ``__dict__``, so every one of them fails
    rather than quietly succeeding in some future debug handler."""
    credential = _credentials.Credential("mob_supersecret", "dev-pat", None)

    assert not dataclasses.is_dataclass(credential)
    with pytest.raises(TypeError):
        dataclasses.asdict(credential)  # type: ignore[call-overload]
    with pytest.raises(TypeError):
        dataclasses.astuple(credential)  # type: ignore[call-overload]
    with pytest.raises(TypeError):
        vars(credential)
    assert not hasattr(credential, "__dict__")
    # And the one deliberate way out still works.
    assert credential.token == "mob_supersecret"


def test_static_credential_cannot_be_dumped_field_by_field():
    source = _credentials.StaticCredential("mob_supersecret")
    assert not dataclasses.is_dataclass(source)
    with pytest.raises(TypeError):
        vars(source)


def test_credential_redacts_itself_in_repr_and_str():
    credential = _credentials.Credential("mob_supersecret", "dev-pat", None)
    assert "mob_supersecret" not in repr(credential)
    assert "mob_supersecret" not in str(credential)
    assert "mob_supersecret" not in f"{credential}"
    assert "REDACTED" in repr(credential)


def test_static_credential_redacts_itself():
    assert "mob_supersecret" not in repr(_credentials.StaticCredential("mob_supersecret"))


def test_resolution_logs_the_source_but_not_the_token(monkeypatch, caplog):
    oidc = make_jwt(time.time() + 3600)
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, oidc)
    with caplog.at_level(logging.DEBUG, logger="bifrost_jupyter"):
        _credentials.CredentialResolver(API_URL).credential()
    assert _credentials.SOURCE_OIDC in caplog.text
    assert oidc not in caplog.text


# --- the process-wide session resolver ------------------------------------


def test_session_resolver_is_shared_and_rebuilt_when_the_api_url_changes():
    first = _credentials.session_resolver(API_URL)
    assert _credentials.session_resolver(API_URL) is first
    other = _credentials.session_resolver("https://other.example")
    assert other is not first
    _credentials.reset_session()
    assert _credentials.session_resolver(API_URL) is not first


def test_session_resolver_resolves_once_per_session(monkeypatch, fake_auth_api):
    """The mint is a *session-start* cost, not a per-request one."""
    monkeypatch.setenv(_credentials.OIDC_TOKEN_ENV_VAR, make_jwt(time.time() + 3600))
    monkeypatch.setenv(_credentials.MINT_PAT_ENV_VAR, "1")
    fake_auth_api.response = SimpleNamespace(
        token="mob_minted", prefix="mob_min", expires_at=time.time() + 86_400
    )

    resolver = _credentials.session_resolver(API_URL)
    for _ in range(5):
        assert resolver.get() == "mob_minted"
    assert len(fake_auth_api.calls) == 1
