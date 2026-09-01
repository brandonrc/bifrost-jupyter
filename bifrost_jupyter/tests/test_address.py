"""In-cluster address derivation — pure function of (id, namespace), no token."""

from urllib.parse import urlsplit

import pytest

from bifrost_jupyter import _address, _profiles


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


def test_connect_snippet_offers_ray_client_as_commented_alternative():
    snippet = _address.connect_snippet("cl-1", "bifrost")
    # The Ray Client (gRPC) path is present only as an advanced, commented option
    # with its owner-pod-only caveat — never as active code.
    assert "ray://cl-1-head-svc.bifrost.svc:10001" in snippet
    for line in snippet.splitlines():
        if "ray://" in line or "ray.init" in line:
            assert line.lstrip().startswith("#"), f"Ray Client line must be commented: {line!r}"
    # Still runnable: the commented lines don't break compilation.
    compile(snippet, "<snippet>", "exec")


def test_dashboard_address_shares_the_jobs_port():
    # Ray serves the dashboard SPA and the Jobs REST API from one server on 8265.
    assert (
        _address.dashboard_address("cl-1", "bifrost") == "http://cl-1-head-svc.bifrost.svc:8265"
    )
    assert _address.dashboard_address("cl-1", "bifrost") == _address.jobs_address("cl-1", "bifrost")


def test_dashboard_address_has_no_trailing_slash():
    # The proxy appends the sub-path itself; a trailing slash here would double up.
    assert not _address.dashboard_address("cl-1", "team-x").endswith("/")


#
# Cluster-id validation. An id is a caller-controlled path segment that gets
# interpolated straight into a host, so this is the control that keeps the target
# pinned to a head service in the configured namespace.
#

# The reviewer's SSRF repro payloads, plus the neighbouring shapes.
MALICIOUS_CLUSTER_IDS = [
    # The original finding: the "?" swallows the "-head-svc.<ns>.svc:8265" suffix
    # into the query string, leaving netloc == "evil.example:9999".
    "evil.example:9999?",
    "127.0.0.1:80?",
    "169.254.169.254?",  # cloud metadata endpoint
    "..",  # traversal
    "../..",
    "a@evil.example",  # host confusion via userinfo
    "%2e%2e",  # percent-encoded traversal (tornado decodes captures)
    "a%2fb",
    "http://evil.example",  # scheme-looking id
    "//evil.example",
    "evil.example#",
    "has space",
    "UPPER",
    "-leading-hyphen",
    "trailing-hyphen-",
    "a" * 64,  # over-long: a DNS label is at most 63 characters
    "",
]


def test_validate_cluster_id_rejects_every_malicious_shape():
    for cluster_id in MALICIOUS_CLUSTER_IDS:
        with pytest.raises(_address.InvalidClusterIdError):
            _address.validate_cluster_id(cluster_id)


def test_validate_cluster_id_accepts_generated_and_plain_ids():
    for cluster_id in ["jl-small-0123456789ab", "cl-1", "a", "a1", "a" * 63]:
        assert _address.validate_cluster_id(cluster_id) == cluster_id


def test_generated_ids_pass_validation():
    # The validator must not reject what the extension itself creates.
    body = _profiles.build_create_cluster(_profiles.SMALL)
    assert _address.validate_cluster_id(body.id) == body.id


def test_address_derivation_refuses_an_unvalidated_id():
    # The backstop: even if a future route forgets the up-front check, no address
    # can be built from a hostile id.
    for derive in (
        _address.head_service_host,
        _address.jobs_address,
        _address.dashboard_address,
        _address.ray_client_address,
        _address.connect_snippet,
    ):
        with pytest.raises(_address.InvalidClusterIdError):
            derive("evil.example:9999?", "bifrost")


def test_the_original_ssrf_repro_can_no_longer_reach_an_attacker_host():
    # Before the fix this produced netloc "evil.example:9999".
    with pytest.raises(_address.InvalidClusterIdError):
        url = _address.dashboard_address("evil.example:9999?", "bifrost")
        assert urlsplit(url).netloc != "evil.example:9999"
