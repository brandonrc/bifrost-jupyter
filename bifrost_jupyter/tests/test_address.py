"""In-cluster address derivation — pure function of (id, namespace), no token."""

from bifrost_jupyter import _address


def test_jobs_address_is_in_cluster_head_service():
    assert _address.jobs_address("cl-1", "bifrost") == "http://cl-1-head-svc.bifrost.svc:8265"


def test_ray_client_address():
    assert _address.ray_client_address("cl-1", "bifrost") == "ray://cl-1-head-svc.bifrost.svc:10001"


def test_namespace_is_honored():
    assert _address.jobs_address("cl-1", "team-x") == "http://cl-1-head-svc.team-x.svc:8265"


def test_connect_snippet_has_no_auth_header_and_is_runnable():
    snippet = _address.connect_snippet("cl-1", "bifrost")
    assert 'JobSubmissionClient("http://cl-1-head-svc.bifrost.svc:8265")' in snippet
    # In-cluster path is gated by the NetworkPolicy, not a token.
    assert "Authorization" not in snippet
    assert "Bearer" not in snippet
    assert "BIFROST_TOKEN" not in snippet
    compile(snippet, "<snippet>", "exec")
