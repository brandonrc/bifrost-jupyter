"""Which project a cluster is started in, and how that is decided.

A ``ClusterSpec`` needs a project, and the panel has no business inventing one.
This module resolves it, in one order, with the reason recorded:

1. ``BIFROST_PROJECT`` when an operator set it. An explicit setting wins, and
   stays useful for a caller whose grant is global (see below).
2. Otherwise, the project the caller holds an ``operator`` (or ``admin``) grant
   in, as ``GET /api/v1/identity`` reports it — when there is exactly one.
3. When there are several, nothing is chosen: the panel offers them and sends
   the one the user picked.
4. When there are none, nothing is chosen and the message says which of the two
   reasons applies — a caller with no project grant at all, or a caller whose
   grant is global and therefore names no project.

**Why this is not a default.** It used to be: the project fell back to the
string ``jupyter``. Every deployment whose projects are named otherwise —
``team-a`` and ``team-b`` on the deployment this was found on — answered its
users' very first Start click with Bifrost's 403 "insufficient permission". The
denial was correct and its message was misleading: the user was not short a
permission, they were pointed at somebody else's project. There is no value
that is right for every user of every deployment, so guessing one trades a
clear question for a wrong answer delivered later, in worse words.

The server's own knowledge is the fix (bifrost#34 added ``projects`` to
identity), and asking it costs one request per session.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

#: The environment variable an operator may set to pin the project.
PROJECT_ENV_VAR = "BIFROST_PROJECT"

#: The roles that may start a cluster in a project. Read (`viewer`) is not
#: enough, and a `developer` grant is read-only on clusters by design, so a
#: project the caller can only look at is not a project the panel may offer.
_STARTING_ROLES = frozenset({"operator", "admin"})

_NO_GRANT = (
    "your account holds no project it may start clusters in — ask an "
    "administrator for an operator grant on a project, or set "
    f"{PROJECT_ENV_VAR}"
)
_GLOBAL_ONLY = (
    "your account's grant is global, so it names no particular project — set "
    f"{PROJECT_ENV_VAR} to the project these clusters belong to"
)
_SERVER_SILENT = (
    "this Bifrost does not report which projects you may use (it predates "
    f"contract 0.3.0) — set {PROJECT_ENV_VAR} to name one"
)
_SEVERAL = "choose which project to start in"


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a project.

    ``project`` is set when one was settled on; otherwise ``message`` says what
    the user or operator has to do, and ``candidates`` lists the projects the
    panel may offer. ``source`` is for the log: ``env``, ``identity``, or
    ``unresolved``.
    """

    project: str | None = None
    candidates: list[str] = field(default_factory=list)
    source: str = "unresolved"
    message: str | None = None

    @property
    def needs_a_choice(self) -> bool:
        """Several projects are available and none has been picked."""
        return self.project is None and bool(self.candidates)


def from_identity(identity: dict | None) -> list[str]:
    """The projects in an identity payload the caller may start clusters in.

    Tolerates an identity from a server that does not carry ``projects`` (an
    empty list), and one whose entries are the wrong shape (skipped) — this
    parses a wire payload, so it is written to survive one.
    """
    if not isinstance(identity, dict):
        return []
    projects = identity.get("projects")
    if not isinstance(projects, list):
        return []
    out: list[str] = []
    for entry in projects:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        roles = entry.get("roles")
        if not isinstance(name, str) or not name or not isinstance(roles, list):
            continue
        if _STARTING_ROLES.intersection(r for r in roles if isinstance(r, str)):
            out.append(name)
    return sorted(set(out))


def resolve(identity: dict | None, *, requested: str | None = None) -> Resolution:
    """Settle the project for a create, or say why it cannot be settled.

    ``requested`` is the project the panel sent, which is only honoured when it
    is one the caller may actually use: a value from the browser is a request,
    not a fact, and Bifrost would refuse it anyway — with a worse message.
    """
    env = (os.environ.get(PROJECT_ENV_VAR) or "").strip()
    available = from_identity(identity)

    if requested:
        if not available or requested in available or requested == env:
            return Resolution(project=requested, candidates=available, source="requested")
        return Resolution(
            candidates=available,
            message=f"you may not start clusters in {requested!r}",
        )

    if env:
        return Resolution(project=env, candidates=available or [env], source="env")

    if len(available) == 1:
        return Resolution(project=available[0], candidates=available, source="identity")
    if len(available) > 1:
        return Resolution(candidates=available, message=_SEVERAL)

    return Resolution(message=_unresolved_message(identity))


def _unresolved_message(identity: dict | None) -> str:
    """Which of the three "no project" situations this is.

    Distinguishing them is the whole point: each has a different fix, and the
    old behaviour collapsed all three into a 403 that named none of them.
    """
    if not isinstance(identity, dict) or "projects" not in identity:
        return _SERVER_SILENT
    roles = identity.get("roles")
    if isinstance(roles, list) and _STARTING_ROLES.intersection(
        r for r in roles if isinstance(r, str)
    ):
        return _GLOBAL_ONLY
    return _NO_GRANT


def parse_identity(raw: bytes | str | None) -> dict | None:
    """The identity payload as a plain dict, or ``None`` when it is unreadable.

    Read raw rather than through the generated client's model on purpose: the
    pinned ``bifrost_client`` predates ``projects`` and pydantic drops unknown
    fields silently, so a model round-trip would lose exactly the field this
    module exists to read. Nothing else here needs the model, and the SDK bump
    can happen on its own schedule.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
