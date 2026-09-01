"""Server-side Bifrost credential resolution (design §4, Wave 2-C Task 9).

The credential never reaches the browser: it is resolved here, inside the user's
notebook pod, and attached to Bifrost control-plane calls by
:mod:`bifrost_jupyter.bifrost`. Nothing in this module puts a token in a return
value that a handler serializes, in a log line, or in an exception message.
:class:`Credential` also refuses the generic ways an object gets dumped —
``repr``/``str`` redact, and it is neither a dataclass nor an attribute bag, so
``asdict``/``astuple``/``vars`` cannot walk past that. Reading ``.token`` is the
one deliberate way out.

Precedence, highest first
------------------------

1. **OIDC access token from the pod environment** — what JupyterHub's
   ``auth_state`` hook injects in the Nebari target. Read from
   ``BIFROST_OIDC_TOKEN_FILE`` (preferred: re-read on every refresh, so a
   rotating file is picked up), else ``BIFROST_OIDC_TOKEN``, else
   ``ACCESS_TOKEN``. There is no env var name mandated upstream — the hook is
   something each deployment writes — so the extension defines its own and also
   accepts ``ACCESS_TOKEN``, the name commonly used in ``auth_state_hook``
   examples.
2. If the pod also carries a **refresh token** and the IdP token endpoint is
   configured, an expired access token is refreshed in place
   (``grant_type=refresh_token``) rather than surfacing a 401.
3. Where audiences don't line up, the access token is exchanged at the IdP for
   a Bifrost-audience token — **RFC 8693** token exchange, the same grant
   Bifrost's own ``internal/auth/flows.go`` speaks (``ExchangeToken``). Note
   the exchange is performed *at the IdP*, not at Bifrost: Bifrost mints
   nothing and only validates the result.
4. Optionally (opt-in, **off by default** — see below) a longer-lived Bifrost
   PAT minted once per session via ``POST /api/v1/auth/tokens``.
5. **Dev fallback**: a pasted ``mob_`` PAT in ``BIFROST_TOKEN``.

OIDC beats the dev PAT: if both are present the OIDC identity wins, because it
is the identity that must match the pod's owner label (see the README).

Why the session PAT mint is opt-in
----------------------------------

Design §4 assumed the session-start PAT mint would be the default. Verified
against the Bifrost source (``internal/api/local_auth.go``,
``internal/auth/local.go``), it is not usable on the production OIDC path:

* ``CreateToken`` calls ``requireLocal()`` first, so on an OIDC-only deployment
  (no ``--local-auth``) the endpoint answers ``404 local auth is not enabled``;
* with local auth on, it mints via ``IssueToken(identity.Subject, …)``, which
  requires a **local user row** whose username equals the OIDC ``sub`` (a
  Keycloak UUID) — absent, that is a 500 ``no such user``;
* and even where such a row exists, the minted PAT authenticates through
  ``identityOf``, whose ``Owner()`` is the local username — the ``sub``, not
  ``preferred_username``. Clusters created with it would be labeled with the
  UUID, so the per-owner NetworkPolicy would stop admitting the notebook pod to
  ``:8265``/``:10001``. Group-derived project roles are lost too.

So the OIDC path keeps the OIDC identity and handles lifetime by refreshing it
(step 2), which is what preserves owner-match. ``BIFROST_MINT_PAT=1`` enables
the mint for deployments where Bifrost's identity for the PAT *is* the pod
owner; a mint that fails degrades to the OIDC token with a warning rather than
breaking the session.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from bifrost_client import ApiClient, ApiException, AuthApi, Configuration
from bifrost_client.models.create_token_request import CreateTokenRequest

#: OIDC access token injected by the JupyterHub ``auth_state`` hook. The *_FILE
#: form is preferred: it is re-read on every refresh, so a rotating file (a
#: refresher sidecar, a projected volume) is picked up without a pod restart,
#: which a spawn-time environment variable can never be.
OIDC_TOKEN_FILE_ENV_VAR = "BIFROST_OIDC_TOKEN_FILE"
OIDC_TOKEN_ENV_VAR = "BIFROST_OIDC_TOKEN"
#: A conventional name for the same thing; the hook is deployment-written, so
#: no name is mandated upstream. Accepted as a fallback.
HUB_OIDC_TOKEN_ENV_VAR = "ACCESS_TOKEN"

#: Optional refresh token, so a long session can renew an expired access token
#: at the IdP instead of surfacing a 401.
OIDC_REFRESH_TOKEN_FILE_ENV_VAR = "BIFROST_OIDC_REFRESH_TOKEN_FILE"
OIDC_REFRESH_TOKEN_ENV_VAR = "BIFROST_OIDC_REFRESH_TOKEN"

#: The IdP's OAuth token endpoint (Keycloak:
#: ``https://<host>/realms/<realm>/protocol/openid-connect/token``). Used by
#: both the refresh grant and the RFC 8693 exchange.
OIDC_TOKEN_URL_ENV_VAR = "BIFROST_OIDC_TOKEN_URL"
OIDC_CLIENT_ID_ENV_VAR = "BIFROST_OIDC_CLIENT_ID"
OIDC_CLIENT_SECRET_ENV_VAR = "BIFROST_OIDC_CLIENT_SECRET"
#: Setting this enables the RFC 8693 exchange: the audience the exchanged token
#: must carry (Bifrost's ``audience`` in its auth config).
OIDC_AUDIENCE_ENV_VAR = "BIFROST_OIDC_AUDIENCE"
OIDC_SCOPE_ENV_VAR = "BIFROST_OIDC_SCOPE"

#: Opt-in session PAT mint (see the module docstring for why it is not default).
MINT_PAT_ENV_VAR = "BIFROST_MINT_PAT"
PAT_TTL_DAYS_ENV_VAR = "BIFROST_PAT_TTL_DAYS"

#: Dev / non-Hub fallback: a pasted ``mob_`` PAT.
TOKEN_ENV_VAR = "BIFROST_TOKEN"

#: RFC 8693 constants, matching ``internal/auth/flows.go``.
GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"

#: Credential sources, reported in logs (never with the token itself).
SOURCE_OIDC = "oidc"
SOURCE_OIDC_REFRESHED = "oidc-refreshed"
SOURCE_OIDC_EXCHANGED = "oidc-exchanged"
SOURCE_MINTED_PAT = "bifrost-pat"
SOURCE_DEV_PAT = "dev-pat"

#: Treat a credential as expired this many seconds early, so a call started just
#: under the wire doesn't land after expiry.
EXPIRY_SKEW_SECS = 60.0

_HTTP_TIMEOUT_SECS = 10.0
_DEFAULT_PAT_TTL_DAYS = 1
_PAT_LABEL = "bifrost-jupyter"

_LOG = logging.getLogger("bifrost_jupyter")

#: Seam for tests: the one place this module performs an outbound HTTP request.
_urlopen = urllib.request.urlopen


class BifrostConfigError(RuntimeError):
    """No credential (or no API URL) is configured at all.

    This is the *bare install* state, not a failure: handlers degrade to the
    "not configured" panel note. The message names environment **variables**,
    never their values.
    """


class CredentialError(RuntimeError):
    """A credential exists but could not be made usable.

    Carries the status and safe message a handler should answer with — an
    expired OIDC token is a 401 the user can act on, a dead IdP is a 502.
    Never carries a token.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Credential:
    """A resolved bearer token plus where it came from and when it dies.

    Deliberately **not** a dataclass, and deliberately ``__slots__``-ed. Both are
    load-bearing for the "the token does not escape" invariant, because
    ``__repr__``/``__str__`` redaction alone is not opacity — it only covers the
    ways a *string* is produced:

    * ``dataclasses.asdict``/``astuple`` walk the declared fields directly and
      would hand back the plaintext token, redaction untouched. A plain class is
      not a dataclass, so both raise ``TypeError``;
    * ``vars()``/``__dict__`` would do the same for any ordinary attribute bag.
      ``__slots__`` means there is no instance ``__dict__`` to dump.

    Reading ``.token`` is then the single deliberate way to get the secret out,
    which is what the two call sites that need it do.
    """

    __slots__ = ("_token", "source", "expires_at")

    def __init__(self, token: str, source: str, expires_at: float | None = None) -> None:
        self._token = token
        self.source = source
        self.expires_at = expires_at

    @property
    def token(self) -> str:
        return self._token

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) >= self.expires_at - EXPIRY_SKEW_SECS

    def __repr__(self) -> str:
        return (
            f"Credential(source={self.source!r}, expires_at={self.expires_at!r}, "
            "token='[REDACTED]')"
        )

    def __str__(self) -> str:
        return self.__repr__()


class CredentialSource(Protocol):
    """What :class:`~bifrost_jupyter.bifrost.BifrostClient` needs of a credential."""

    @property
    def refreshable(self) -> bool:
        """Whether :meth:`invalidate` + :meth:`get` can produce a *new* token."""

    def get(self) -> str:
        """The current bearer token."""

    def invalidate(self) -> None:
        """Drop any cached token so the next :meth:`get` re-resolves."""


class StaticCredential:
    """A fixed token — the dev/direct case and the shape tests construct.

    ``__slots__`` for the same reason :class:`Credential` has it: no instance
    ``__dict__``, so ``vars()`` cannot dump the token past ``__repr__``.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def refreshable(self) -> bool:
        return False

    def get(self) -> str:
        return self._token

    def invalidate(self) -> None:
        return None

    def __repr__(self) -> str:
        return "StaticCredential(token='[REDACTED]')"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _env_flag(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def _read_secret(file_var: str, *env_vars: str) -> str | None:
    """A secret from a file (preferred, re-read every time) else an env var."""
    path = _env(file_var)
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            # The path is operator-set config, not caller input, so naming it is
            # safe and is what an operator needs to fix the deployment.
            _LOG.warning(
                "bifrost: %s=%s is unreadable; falling back to the environment", file_var, path
            )
            value = ""
        if value:
            return value
    for var in env_vars:
        value = _env(var)
        if value:
            return value
    return None


def read_oidc_token() -> str | None:
    """The pod's OIDC access token, or ``None`` if the hook injected none."""
    return _read_secret(OIDC_TOKEN_FILE_ENV_VAR, OIDC_TOKEN_ENV_VAR, HUB_OIDC_TOKEN_ENV_VAR)


def read_refresh_token() -> str | None:
    """The pod's OIDC refresh token, if the deployment injects one."""
    return _read_secret(OIDC_REFRESH_TOKEN_FILE_ENV_VAR, OIDC_REFRESH_TOKEN_ENV_VAR)


def jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a JWT, without verifying anything.

    Signature/issuer/audience validation is Bifrost's job — this only needs to
    know *when to refresh*, and a token we cannot parse (a ``mob_`` PAT, say)
    simply has no locally known expiry.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def _post_form(url: str, form: dict[str, str], what: str) -> dict[str, Any]:
    """POST an ``application/x-www-form-urlencoded`` body to the IdP.

    https only: the body carries the user's live credential, and the response is
    a bearer token — over cleartext a network attacker reads one and substitutes
    the other. (Bifrost refuses a non-https issuer for the same reason.)

    Every failure is reduced to a fixed message. The exception is deliberately
    *not* chained and never formatted into the message: an IdP error body can
    echo request parameters, and those include ``subject_token``.
    """
    if urllib.parse.urlsplit(url).scheme != "https":
        raise CredentialError(500, f"{OIDC_TOKEN_URL_ENV_VAR} must be an https:// URL")
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with _urlopen(request, timeout=_HTTP_TIMEOUT_SECS) as response:
            raw = response.read()
    except urllib.error.HTTPError:
        # The status is safe to report; the body is not.
        raise CredentialError(502, f"{what} was rejected by the identity provider") from None
    except (urllib.error.URLError, OSError):
        raise CredentialError(502, f"{what} could not reach the identity provider") from None
    try:
        payload = json.loads(raw)
    except ValueError:
        raise CredentialError(502, f"{what} returned an unreadable response") from None
    if not isinstance(payload, dict):
        raise CredentialError(502, f"{what} returned an unreadable response")
    return payload


def _credential_from_token_response(payload: dict[str, Any], source: str, what: str) -> Credential:
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise CredentialError(502, f"{what} returned no access_token")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at: float | None = time.time() + float(expires_in)
    else:
        expires_at = jwt_expiry(token)
    return Credential(token, source, expires_at)


def _client_form() -> dict[str, str]:
    client_id = _env(OIDC_CLIENT_ID_ENV_VAR)
    if not client_id:
        raise CredentialError(500, f"{OIDC_CLIENT_ID_ENV_VAR} is not set")
    form = {"client_id": client_id}
    secret = _env(OIDC_CLIENT_SECRET_ENV_VAR)
    if secret:
        form["client_secret"] = secret
    return form


def _token_url() -> str:
    url = _env(OIDC_TOKEN_URL_ENV_VAR)
    if not url:
        raise CredentialError(500, f"{OIDC_TOKEN_URL_ENV_VAR} is not set")
    return url


def refresh_configured() -> bool:
    return bool(_env(OIDC_TOKEN_URL_ENV_VAR)) and read_refresh_token() is not None


def exchange_configured() -> bool:
    """The RFC 8693 exchange runs only when an audience is requested."""
    return bool(_env(OIDC_AUDIENCE_ENV_VAR))


def refresh_access_token(refresh_token: str) -> Credential:
    """OAuth 2.0 refresh grant against the IdP token endpoint."""
    form = _client_form()
    form.update({"grant_type": "refresh_token", "refresh_token": refresh_token})
    payload = _post_form(_token_url(), form, "the OIDC token refresh")
    return _credential_from_token_response(payload, SOURCE_OIDC_REFRESHED, "the OIDC token refresh")


def exchange_token(subject_token: str) -> Credential:
    """RFC 8693 token exchange, mirroring ``auth.ExchangeToken``'s form.

    The exchanged token keeps the *user* as subject — that is the whole point:
    the identity Bifrost stamps as ``ClusterSpec.owner`` must stay the pod's
    owner.
    """
    form = _client_form()
    form.update(
        {
            "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
            "subject_token": subject_token,
            "subject_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            "requested_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            "audience": _env(OIDC_AUDIENCE_ENV_VAR),
        }
    )
    scope = _env(OIDC_SCOPE_ENV_VAR)
    if scope:
        form["scope"] = scope
    payload = _post_form(_token_url(), form, "the RFC 8693 token exchange")
    return _credential_from_token_response(payload, SOURCE_OIDC_EXCHANGED, "the token exchange")


def mint_pat_enabled() -> bool:
    return _env_flag(MINT_PAT_ENV_VAR)


def _pat_ttl_days() -> int:
    raw = _env(PAT_TTL_DAYS_ENV_VAR)
    if not raw:
        return _DEFAULT_PAT_TTL_DAYS
    try:
        days = int(raw)
    except ValueError:
        return _DEFAULT_PAT_TTL_DAYS
    # Bifrost caps PAT lifetime at 90 days and rejects 0 with a 400.
    return min(max(days, 1), 90)


def mint_session_pat(api_url: str, bearer: Credential) -> Credential | None:
    """Trade ``bearer`` for a Bifrost PAT, or ``None`` if that isn't possible.

    Returning ``None`` (not raising) is deliberate: on an OIDC deployment this
    endpoint is frequently unavailable — see the module docstring — and the
    session must continue on the OIDC token rather than fail.
    """
    try:
        api = AuthApi(ApiClient(Configuration(host=api_url, access_token=bearer.token)))
        response = api.create_token(
            CreateTokenRequest(label=_PAT_LABEL, expires_in_days=_pat_ttl_days())
        )
    except ApiException as exc:
        # Status only. ApiException stringifies its response body, which is
        # upstream text this extension must not relay or log.
        _LOG.warning(
            "bifrost: session PAT mint returned HTTP %s; continuing with the %s credential "
            "(POST /api/v1/auth/tokens needs local auth enabled and a local user for the "
            "token subject)",
            exc.status,
            bearer.source,
        )
        return None
    except Exception as exc:  # never let a mint failure end the session
        _LOG.warning(
            "bifrost: session PAT mint failed (%s); continuing with the %s credential",
            type(exc).__name__,
            bearer.source,
        )
        return None
    return Credential(response.token, SOURCE_MINTED_PAT, float(response.expires_at))


#: What the user is told when their pod's OIDC token has gone stale. Actionable
#: on purpose: this is the failure the design calls out as never acceptable to
#: surface as a mystery 401.
EXPIRED_OIDC_MESSAGE = (
    "the notebook's OIDC access token has expired and could not be refreshed — "
    "restart your server to pick up a fresh token, or ask an admin to enable "
    "refresh-token injection (see 'Authentication' in the bifrost-jupyter README)"
)

NO_CREDENTIAL_MESSAGE = (
    f"no Bifrost credential is configured: set {OIDC_TOKEN_FILE_ENV_VAR} or "
    f"{OIDC_TOKEN_ENV_VAR} (production — injected by the JupyterHub auth_state hook), "
    f"or {TOKEN_ENV_VAR} (dev PAT)"
)


class CredentialResolver:
    """The session's credential, resolved lazily and re-resolved on expiry.

    One instance per server process (see :func:`session_resolver`), so the
    session-start work — the exchange, and the PAT mint when enabled — happens
    once, not per request. Tornado runs single-threaded, so no locking.
    """

    def __init__(self, api_url: str) -> None:
        self.api_url = api_url
        self._cached: Credential | None = None

    @property
    def refreshable(self) -> bool:
        return True

    def get(self) -> str:
        return self.credential().token

    def credential(self) -> Credential:
        cached = self._cached
        if cached is not None and not cached.is_expired():
            return cached
        resolved = self._resolve()
        self._cached = resolved
        _LOG.debug("bifrost: credential resolved from %s", resolved.source)
        return resolved

    def invalidate(self) -> None:
        self._cached = None

    def _resolve(self) -> Credential:
        oidc = read_oidc_token()
        if oidc is not None:
            return self._from_oidc(oidc)
        dev = _env(TOKEN_ENV_VAR)
        if dev:
            return Credential(dev, SOURCE_DEV_PAT, jwt_expiry(dev))
        raise BifrostConfigError(NO_CREDENTIAL_MESSAGE)

    def _from_oidc(self, access_token: str) -> Credential:
        subject = Credential(access_token, SOURCE_OIDC, jwt_expiry(access_token))
        if subject.is_expired():
            # A stale pre-spawn snapshot: renew it at the IdP if we can, and if
            # we can't, say so in terms the user can act on rather than letting
            # Bifrost answer an unexplained 401.
            refresh_token = read_refresh_token() if refresh_configured() else None
            if refresh_token is None:
                raise CredentialError(401, EXPIRED_OIDC_MESSAGE)
            subject = refresh_access_token(refresh_token)
        if exchange_configured():
            subject = exchange_token(subject.token)
        if mint_pat_enabled():
            minted = mint_session_pat(self.api_url, subject)
            if minted is not None:
                return minted
        return subject


_SESSION: CredentialResolver | None = None


def session_resolver(api_url: str) -> CredentialResolver:
    """The process-wide resolver — "session start" is its first resolution."""
    global _SESSION
    if _SESSION is None or _SESSION.api_url != api_url:
        _SESSION = CredentialResolver(api_url)
    return _SESSION


def reset_session() -> None:
    """Drop the process-wide resolver (tests, and a config change)."""
    global _SESSION
    _SESSION = None
