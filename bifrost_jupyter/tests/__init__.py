"""Python unit tests for bifrost_jupyter."""

#: Hostile cluster ids that a client can actually get *onto the wire* as a single
#: path segment, shared by every ``{id}``-taking route's regression test.
#:
#: These are the SSRF repro payloads: before ``_address.validate_cluster_id``,
#: ``<id>-head-svc.<ns>.svc`` interpolation let a caller restructure the URL the
#: server connects to — ``evil.example:9999?`` swallowed the intended suffix into
#: the query string, leaving an attacker-chosen ``netloc``. Every route must
#: answer 400 for each of these *and* issue no upstream request.
#:
#: Shapes containing ``/`` (``http://evil.example``, ``//evil.example``) and the
#: empty id are unit-tested in ``test_address.py`` instead: they cannot survive
#: as one path segment, so they never reach a handler.
ROUTE_SSRF_IDS = [
    # The original finding: "?" swallows "-head-svc.<ns>.svc:8265" into the query
    # string, so the connection target becomes "evil.example:9999".
    "evil.example:9999?",
    "127.0.0.1:80?",
    "169.254.169.254?",  # cloud metadata endpoint
    "evil.example#",
    "a@evil.example",  # host confusion via userinfo
    "..",  # traversal
    "%2e%2e",  # percent-encoded traversal (tornado decodes captures after routing)
    "a%2fb",  # percent-encoded separator
    "UPPER",
    "has space",
    "-leading-hyphen",
    "trailing-hyphen-",
    "a" * 64,  # over-long: a DNS label is at most 63 characters
]
