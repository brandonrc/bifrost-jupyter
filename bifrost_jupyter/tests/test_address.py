"""Gateway address helpers — must never embed the bearer token."""

from bifrost_jupyter import _address

TOKEN = "mob_supersecrettoken"


def test_jobs_address_is_https_host():
    assert _address.jobs_address("cl-abc.gw.example") == "https://cl-abc.gw.example"


def test_headers_hint_names_env_var_not_token():
    hint = _address.headers_hint()
    assert hint == {"Authorization": "Bearer ${BIFROST_TOKEN}"}
    assert TOKEN not in repr(hint)


def test_connect_snippet_reads_token_from_env_and_is_runnable():
    snippet = _address.connect_snippet("cl-abc.gw.example")
    assert "https://cl-abc.gw.example" in snippet
    assert "JobSubmissionClient" in snippet
    # The token is read from the environment at runtime, never baked in.
    assert "os.environ['BIFROST_TOKEN']" in snippet
    assert TOKEN not in snippet
    # Snippet is syntactically valid Python.
    compile(snippet, "<snippet>", "exec")
