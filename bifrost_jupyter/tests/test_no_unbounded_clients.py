"""Structural guards: the two defects Task 11 fixed must not come back.

Both were the same shape — a property of a *mechanism* (the generated client's
absent timeout; tornado's single-threaded IOLoop) that every call site inherits
silently. Fixing the call sites that existed would have left the next route to
rediscover them, so the fixes are structural: one bounded API-client factory,
and blocking work confined to an executor helper.

These read the package's own source with ``ast`` and fail if a new route can
reintroduce either defect. They are deliberately about *shape*, not behaviour:
the behavioural tests live next door, but they only cover routes someone
remembered to write a test for.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent

#: The module allowed to construct a raw ``ApiClient`` — it is the one that
#: makes it bounded.
CHOKEPOINT_MODULE = "_apiclient.py"

#: HTTP verbs tornado dispatches to.
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}

#: Names that reach a blocking Bifrost call. A sync handler method touching any
#: of these would run it on the IOLoop thread and stall the whole notebook
#: server — kernel traffic, file saves, every other extension.
BLOCKING_NAMES = {"client_from_env", "_client", "_blocking", "_write_client_or_fail"}


def _package_modules():
    return sorted(p for p in PACKAGE.glob("*.py") if p.name != "__init__.py")


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_only_the_chokepoint_constructs_an_api_client():
    """A raw ``ApiClient`` is unbounded — urllib3 waits forever. Anything that
    needs one must go through ``_apiclient.bounded_api_client``."""
    offenders = []
    for path in _package_modules():
        if path.name == CHOKEPOINT_MODULE:
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "ApiClient":
                    offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "raw ApiClient construction outside "
        f"{CHOKEPOINT_MODULE}: {offenders}. Use bounded_api_client() — a raw "
        "client has no request timeout, so a connected-but-silent Bifrost pins "
        "the calling thread for the life of the server."
    )


def test_no_module_imports_api_client_except_the_chokepoint():
    """Belt and braces: catch an aliased import before it becomes a call."""
    offenders = []
    for path in _package_modules():
        if path.name == CHOKEPOINT_MODULE:
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module == "bifrost_client":
                for alias in node.names:
                    if alias.name == "ApiClient":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"ApiClient imported outside {CHOKEPOINT_MODULE}: {offenders}"


def finds_blocking_call(node: ast.AST) -> list[str]:
    """The shape rule itself: which blocking names does this function body touch?

    Deliberately the *only* implementation. Both the guard that scans
    ``handlers.py`` and the regression test that proves the guard still bites
    call this, so neutering it fails both at once. A second, hand-written copy
    inside the regression test would leave that test blind to a regression in
    the guard's own matching — which is exactly what happened before.
    """
    used = {
        child.attr if isinstance(child, ast.Attribute) else child.id
        for child in ast.walk(node)
        if isinstance(child, (ast.Attribute, ast.Name))
    }
    return sorted(used & BLOCKING_NAMES)


def scan_for_sync_blocking(tree: ast.Module) -> list[str]:
    """The whole rule — scan *and* match — over any parsed module.

    The guard runs this against ``handlers.py``; the regression test runs the
    identical function against a synthetic module that carries the defect. So a
    break anywhere in this path — the class walk, the verb filter, the
    async check, or :func:`finds_blocking_call` — fails the regression test,
    rather than only a break in the part someone remembered to copy.
    """
    offenders = []
    for cls_name, node, is_async in _handler_methods(tree):
        if is_async:
            continue
        blocking = finds_blocking_call(node)
        if blocking:
            offenders.append(f"{cls_name}.{node.name} (line {node.lineno}) uses {blocking}")
    return offenders


def _handler_methods(tree: ast.Module):
    """Every HTTP-verb method defined in ``tree``, with its async-ness."""
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in HTTP_VERBS:
                    yield cls.name, node, isinstance(node, ast.AsyncFunctionDef)


def test_no_synchronous_handler_touches_a_blocking_call():
    """The Task 11 defect, as a shape rule.

    A sync ``def get``/``def post`` that reaches a Bifrost client runs the call
    on tornado's IOLoop thread. jupyter-server is single-threaded, so that
    freezes the user's entire Lab session — not just this panel — for as long as
    Bifrost takes to answer.
    """
    offenders = scan_for_sync_blocking(_parse(PACKAGE / "handlers.py"))

    assert not offenders, (
        "synchronous handler methods reaching a blocking Bifrost call: "
        f"{offenders}. Make the method `async def` and await it through "
        "`self._blocking(...)`, which runs it on the IOLoop's thread pool."
    )


def test_the_guard_would_catch_a_regression():
    """The guard above passes trivially if its rule never matches anything.

    Parse a synthetic handler carrying the exact defect and confirm
    :func:`finds_blocking_call` — *the same function the guard calls*, not a
    second copy of its logic — flags it.

    That sharing is the whole point, and an earlier version of this test got it
    wrong: it re-implemented the extraction against the synthetic snippet, so
    the two copies had only ``BLOCKING_NAMES`` in common. Neutering the real
    guard's matching left this test green, which is precisely the blindness it
    exists to prevent. Break the shared function now and both fail together.
    """
    source = (
        "class BadHandler(_BifrostHandler):\n"
        "    def get(self):\n"
        "        client = client_from_env()\n"
        "        return client.list_clusters()\n"
    )
    offenders = scan_for_sync_blocking(ast.parse(source))
    assert offenders == ["BadHandler.get (line 2) uses ['client_from_env']"], (
        f"the rule no longer detects the defect it exists for: {offenders}"
    )


def test_the_guard_does_not_flag_a_clean_handler():
    """The other half: a rule that matched everything would also be useless.

    Covers both shapes the codebase actually uses — a sync method that touches
    no blocking name, and an async one that legitimately does.
    """
    sync_but_clean = (
        "class FineHandler(_BifrostHandler):\n"
        "    def get(self):\n"
        "        return self.finish(json.dumps({'ok': True}))\n"
    )
    async_and_blocking = (
        "class FineAsyncHandler(_BifrostHandler):\n"
        "    async def get(self):\n"
        "        client = await self._client()\n"
        "        return await self._blocking(client.list_clusters)\n"
    )
    assert scan_for_sync_blocking(ast.parse(sync_but_clean)) == []
    assert scan_for_sync_blocking(ast.parse(async_and_blocking)) == []


def test_at_least_one_handler_is_async_so_the_scan_found_something():
    """Guards against the scan silently walking an empty set of methods."""
    methods = list(_handler_methods(_parse(PACKAGE / "handlers.py")))
    assert methods, "no handler methods found — the AST scan is looking in the wrong place"
    assert any(is_async for _, _, is_async in methods)


@pytest.mark.parametrize("name", ["_apiclient.py", "handlers.py"])
def test_the_scanned_files_exist(name):
    assert (PACKAGE / name).is_file()
