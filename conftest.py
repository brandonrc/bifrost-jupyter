import os

import pytest

from bifrost_jupyter import _credentials

pytest_plugins = ("pytest_jupyter.jupyter_server", )


@pytest.fixture
def jp_server_config(jp_server_config):
    return {
        "ServerApp": {
            "jpserver_extensions": {"bifrost_jupyter": True},
            # Test against a server which requires authentication on all endpoints
            "allow_unauthenticated_access": False,
        }
    }


#: Every environment variable the credential resolver reads. Cleared before each
#: test so an ambient one (a developer's real ACCESS_TOKEN, say) can never make a
#: test pass — or fail — for reasons that have nothing to do with the test.
_CREDENTIAL_ENV_VARS = (
    _credentials.OIDC_TOKEN_FILE_ENV_VAR,
    _credentials.OIDC_TOKEN_ENV_VAR,
    _credentials.HUB_OIDC_TOKEN_ENV_VAR,
    _credentials.OIDC_REFRESH_TOKEN_FILE_ENV_VAR,
    _credentials.OIDC_REFRESH_TOKEN_ENV_VAR,
    _credentials.OIDC_TOKEN_URL_ENV_VAR,
    _credentials.OIDC_CLIENT_ID_ENV_VAR,
    _credentials.OIDC_CLIENT_SECRET_ENV_VAR,
    _credentials.OIDC_AUDIENCE_ENV_VAR,
    _credentials.OIDC_SCOPE_ENV_VAR,
    _credentials.MINT_PAT_ENV_VAR,
    _credentials.PAT_TTL_DAYS_ENV_VAR,
    _credentials.TOKEN_ENV_VAR,
    "BIFROST_API_URL",
)


#: The credential environment as the process started, captured before any test
#: can clear it. Live tests are handed this back; see below.
_AMBIENT_ENV = {var: os.environ.get(var) for var in _CREDENTIAL_ENV_VARS}


@pytest.fixture(autouse=True)
def clean_credential_env(monkeypatch, request):
    """A clean credential environment and a fresh session resolver per test.

    The resolver is process-wide on purpose (the session-start exchange/mint must
    happen once, not per request), so it has to be reset between tests or one
    test's cached credential leaks into the next.

    A test marked ``live`` is the exception, and has to be: it exists to drive a
    real Bifrost, which it can only do with the environment the developer or CI
    set. Scrubbing it there made every live test answer "not configured" — a
    409 that reads like a product bug and is really this fixture. So those tests
    get the ambient environment restored rather than removed, and everything
    else keeps the hermetic default.
    """
    live = request.node.get_closest_marker("live") is not None
    for var in _CREDENTIAL_ENV_VARS:
        ambient = _AMBIENT_ENV.get(var)
        if live and ambient is not None:
            monkeypatch.setenv(var, ambient)
        else:
            monkeypatch.delenv(var, raising=False)
    _credentials.reset_session()
    yield
    _credentials.reset_session()
