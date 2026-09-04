"""Which project a cluster is started in, and why.

The resolution order is the whole content of this module, so each rule gets a
test: an explicit setting wins, one grant needs no asking, several need a
choice, and none has three different reasons with three different fixes. The
last part is what the old hardcoded default destroyed — every one of those
situations arrived as the same 403.
"""

import pytest

from bifrost_jupyter import _projects


def identity(*projects, roles=("developer",), **kwargs):
    """An identity payload with `projects` entries as `(name, *roles)` tuples."""
    return {
        "subject": "alice",
        "groups": [],
        "roles": list(roles),
        "projects": [{"name": p[0], "roles": list(p[1:]) or ["operator"]} for p in projects],
        **kwargs,
    }


def test_an_explicit_setting_wins(monkeypatch):
    monkeypatch.setenv(_projects.PROJECT_ENV_VAR, "team-x")
    got = _projects.resolve(identity(("team-a", "operator")))
    assert got.project == "team-x"
    assert got.source == "env"


def test_one_grant_needs_no_asking(monkeypatch):
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    got = _projects.resolve(identity(("team-a", "operator")))
    assert got.project == "team-a"
    assert got.source == "identity"
    assert not got.needs_a_choice


def test_several_grants_are_offered_not_guessed(monkeypatch):
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    got = _projects.resolve(identity(("team-b", "operator"), ("team-a", "admin")))
    assert got.project is None
    assert got.candidates == ["team-a", "team-b"]  # sorted, so the panel is stable
    assert got.needs_a_choice
    assert "choose" in got.message


def test_a_project_the_caller_only_reads_is_not_offered(monkeypatch):
    """A `viewer` or `developer` grant cannot start a cluster, so it is not a
    candidate — offering it would move the 403 rather than remove it."""
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    got = _projects.resolve(identity(("team-a", "viewer"), ("team-b", "developer")))
    assert got.project is None
    assert got.candidates == []
    assert "no project it may start clusters in" in got.message


def test_a_global_grant_names_no_project(monkeypatch):
    """A global operator may act anywhere, so identity names nothing. Saying
    "you have no permission" would be wrong; saying "name one" is right."""
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    got = _projects.resolve(identity(roles=("admin",)))
    assert got.project is None
    assert _projects.PROJECT_ENV_VAR in got.message
    assert "global" in got.message


def test_a_server_that_does_not_report_projects_says_so(monkeypatch):
    """An older Bifrost carries no `projects` field. That is a third situation
    with its own fix, and it must not be reported as a missing grant."""
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    for stale in ({"subject": "alice", "roles": ["developer"], "groups": []}, None, "nonsense"):
        got = _projects.resolve(stale if not isinstance(stale, str) else None)
        assert got.project is None
        assert "predates" in got.message


def test_a_requested_project_is_honoured_when_it_is_the_callers(monkeypatch):
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    who = identity(("team-a", "operator"), ("team-b", "operator"))
    assert _projects.resolve(who, requested="team-b").project == "team-b"


def test_a_requested_project_the_caller_lacks_is_refused(monkeypatch):
    """A value from the browser is a request, not a fact. Bifrost would refuse
    it too — with a message about permissions rather than about the project."""
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    got = _projects.resolve(identity(("team-a", "operator")), requested="team-secret")
    assert got.project is None
    assert "team-secret" in got.message


def test_a_requested_project_is_honoured_against_a_silent_server(monkeypatch):
    """With no list to check against, the request stands: refusing it would
    make the extension unusable on any server that cannot answer."""
    monkeypatch.delenv(_projects.PROJECT_ENV_VAR, raising=False)
    got = _projects.resolve({"subject": "alice", "roles": [], "groups": []}, requested="team-a")
    assert got.project == "team-a"


@pytest.mark.parametrize(
    "payload",
    [
        {"projects": "not-a-list"},
        {"projects": [None, 3, "x"]},
        {"projects": [{"name": "", "roles": ["operator"]}]},
        {"projects": [{"name": "team-a"}]},
        {"projects": [{"name": "team-a", "roles": "operator"}]},
        {"projects": [{"name": "team-a", "roles": [None]}]},
    ],
)
def test_a_malformed_payload_yields_no_candidates(payload):
    """This parses a wire payload, so it is written to survive one."""
    assert _projects.from_identity(payload) == []


def test_duplicate_entries_collapse():
    who = {"projects": [
        {"name": "team-a", "roles": ["operator"]},
        {"name": "team-a", "roles": ["admin"]},
    ]}
    assert _projects.from_identity(who) == ["team-a"]


@pytest.mark.parametrize("raw", [b"not json", "", b"[]", b"null", None])
def test_unreadable_identity_parses_to_none(raw):
    assert _projects.parse_identity(raw) is None


def test_identity_parses_from_bytes():
    assert _projects.parse_identity(b'{"subject":"a"}') == {"subject": "a"}
