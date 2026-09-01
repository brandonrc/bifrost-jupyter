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


@pytest.fixture(autouse=True)
def clean_credential_env(monkeypatch):
    """A clean credential environment and a fresh session resolver per test.

    The resolver is process-wide on purpose (the session-start exchange/mint must
    happen once, not per request), so it has to be reset between tests or one
    test's cached credential leaks into the next.
    """
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    _credentials.reset_session()
    yield
    _credentials.reset_session()
