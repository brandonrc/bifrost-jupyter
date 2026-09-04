"""bifrost.py wrapper: credential attachment, refresh-on-401, safe errors."""

import socket
import threading
import time
from types import SimpleNamespace

import pytest
from bifrost_client import ApiException

from bifrost_jupyter import _credentials, bifrost
from bifrost_jupyter._profiles import build_create_cluster
from bifrost_jupyter.tests.test_credentials import make_jwt

API_URL = "https://bifrost.example"
TOKEN = "bfr_supersecrettoken"


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
    client._clusters.create_cluster = lambda body, **kw: captured.setdefault("body", body)

    body = build_create_cluster("small", project="team-a")
    client.create_cluster(body)
    assert captured["body"] is body


def test_list_clusters_passes_through():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    views = [object(), object()]
    client._clusters.list_clusters = lambda **kw: views
    assert client.list_clusters() is views


@pytest.mark.parametrize("op", ["delete_cluster", "suspend_cluster", "resume_cluster"])
def test_lifecycle_ops_pass_id_through(op):
    client = bifrost.BifrostClient(API_URL, TOKEN)
    captured = {}
    setattr(client._clusters, op, lambda cluster_id, **kw: captured.setdefault("id", cluster_id))

    getattr(client, op)("cl-42")
    assert captured["id"] == "cl-42"


@pytest.mark.parametrize("op", ["delete_cluster", "suspend_cluster", "resume_cluster"])
def test_lifecycle_ops_translate_error(op):
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser(_cluster_id, **_kw):
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

    def raiser(**_kw):
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

    def raiser(_cluster_id, **_kw):
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

    def flaky(**_kw):
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

    def always_401(_cluster_id, **_kw):
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

    def always_401(**_kw):
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

    def always_401(**_kw):
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

    def raiser(_cluster_id, **_kw):
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

    def raiser(_body, **_kw):
        raise ApiException(status=403, reason="upstream", body="SECRET internal detail")

    client._clusters.create_cluster = raiser

    with pytest.raises(bifrost.BifrostAPIError) as exc_info:
        client.create_cluster(build_create_cluster("small", project="team-a"))
    assert exc_info.value.status == 403
    assert "operator" in exc_info.value.message
    assert "SECRET" not in exc_info.value.message


def test_read_403_stays_generic():
    """The operator hint belongs on lifecycle calls; a Read 403 means something
    else (wrong project), so it must not claim the wrong remedy."""
    client = bifrost.BifrostClient(API_URL, TOKEN)

    def raiser(**_kw):
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


# --- every outbound call is bounded (Task 11) ------------------------------
#
# The generated client leaves ``timeout = None`` unless a caller passes
# ``_request_timeout`` (bifrost_client/rest.py), i.e. urllib3 waits forever. Off
# the IOLoop that no longer freezes Lab, but it still pins a pool thread for the
# life of the server, and enough of those starve the pool back into a freeze.

_OPS = [
    ("create_cluster", lambda c: c.create_cluster(build_create_cluster("small", project="team-a"))),
    ("get_cluster", lambda c: c.get_cluster("cl-1")),
    ("list_clusters", lambda c: c.list_clusters()),
    ("delete_cluster", lambda c: c.delete_cluster("cl-1")),
    ("suspend_cluster", lambda c: c.suspend_cluster("cl-1")),
    ("resume_cluster", lambda c: c.resume_cluster("cl-1")),
]


def _record_transport(client):
    """Replace the client's transport, returning what each call reaches it with.

    Stubbed at ``rest_client.request`` rather than at the endpoint method, so
    everything above it — the generated endpoint, ``param_serialize``,
    ``call_api``, and the bounded override — runs for real. This observes the
    timeout that would actually be handed to urllib3, not one a test fake was
    told to expect.
    """
    calls = []

    def fake_request(method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        calls.append({"method": method, "url": url, "_request_timeout": _request_timeout})
        return SimpleNamespace(status=200, data=b"[]", getheaders=lambda: {}, read=lambda: b"[]")

    client._clusters.api_client.rest_client.request = fake_request
    return calls


@pytest.mark.parametrize("name,invoke", _OPS, ids=[n for n, _ in _OPS])
def test_every_bifrost_call_reaches_the_transport_bounded(name, invoke):
    """Not "every call site remembers the argument" — the transport never sees a
    ``None`` timeout, whichever endpoint is driven."""
    client = bifrost.BifrostClient(API_URL, TOKEN)
    calls = _record_transport(client)

    try:
        invoke(client)
    except Exception:  # noqa: BLE001 - deserialising the stub body may fail; the call went out
        pass

    assert calls, f"{name} issued no request"
    assert calls[0]["_request_timeout"] == bifrost.REQUEST_TIMEOUT_SECONDS, (
        f"{name} reached the transport unbounded — urllib3 would wait forever"
    )


def test_the_timeout_is_configurable_per_client():
    client = bifrost.BifrostClient(API_URL, TOKEN, timeout=2.5)
    calls = _record_transport(client)
    try:
        client.list_clusters()
    except Exception:  # noqa: BLE001
        pass
    assert calls[0]["_request_timeout"] == 2.5


def test_an_endpoint_that_never_passes_a_timeout_is_still_bounded():
    """The structural guarantee as a test: the bound does not depend on the
    caller. This is what a future endpoint wrapper gets for free, and what a
    per-call-site argument could never give it."""
    client = bifrost.BifrostClient(API_URL, TOKEN)
    calls = _record_transport(client)

    # A caller that knows nothing about timeouts, as a new wrapper would be.
    client._clusters.api_client.call_api("GET", f"{API_URL}/api/v1/anything")

    assert calls[0]["_request_timeout"] == bifrost.REQUEST_TIMEOUT_SECONDS


def test_an_explicit_timeout_still_wins():
    client = bifrost.BifrostClient(API_URL, TOKEN)
    calls = _record_transport(client)
    client._clusters.api_client.call_api("GET", f"{API_URL}/x", _request_timeout=0.25)
    assert calls[0]["_request_timeout"] == 0.25


def _call_against_silent_server(timeout, join_seconds):
    """Call ``list_clusters`` against a real socket that accepts and never answers.

    Returns ``(finished, outcome)``. The server completes the TCP handshake and
    then says nothing, which is the failure mode a request timeout exists for —
    a refused connection or a DNS failure returns promptly on its own.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    accepted = []

    def accept_and_stay_silent():
        try:
            while True:
                conn, _ = listener.accept()
                accepted.append(conn)  # held open, never written to
        except OSError:
            return

    threading.Thread(target=accept_and_stay_silent, daemon=True).start()

    client = bifrost.BifrostClient(f"http://127.0.0.1:{port}", TOKEN, timeout=timeout)
    outcome = []

    def call():
        try:
            client.list_clusters()
            outcome.append("returned")
        except BaseException as exc:  # noqa: BLE001 - any bounded failure is a pass
            outcome.append(type(exc).__name__)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout=join_seconds)
    finished = not worker.is_alive()
    listener.close()
    for conn in accepted:
        conn.close()
    return finished, outcome


def test_an_unbounded_call_really_does_hang():
    """The control arm: with no request timeout the call does not come back.

    This is the defect the timeout closes, demonstrated rather than asserted —
    the generated client leaves ``timeout = None`` when ``_request_timeout`` is
    falsy, and urllib3 then waits indefinitely. The thread is left running
    (daemon, so it does not hold the suite up)."""
    finished, outcome = _call_against_silent_server(timeout=None, join_seconds=5.0)
    assert not finished, f"expected no answer within 5s, got {outcome}"


def test_a_connected_but_silent_server_does_not_hang_forever():
    """The fix: the same call, bounded."""
    started = time.monotonic()
    finished, outcome = _call_against_silent_server(
        timeout=1.0,
        # Generously more than the 1s budget (urllib3 retries), far less than forever.
        join_seconds=20.0,
    )
    elapsed = time.monotonic() - started
    assert finished, (
        "the call was still running after 20s against a silent server — "
        "an unbounded request would pin this thread for the life of the server"
    )
    assert outcome and outcome[0] != "returned", f"unexpected success: {outcome}"
    print(f"\nbounded failure after {elapsed:.1f}s: {outcome[0]}")
