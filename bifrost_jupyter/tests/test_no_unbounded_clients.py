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


def _handler_methods():
    """Every HTTP-verb method defined in ``handlers.py``, with its async-ness."""
    tree = _parse(PACKAGE / "handlers.py")
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
    offenders = []
    for cls_name, node, is_async in _handler_methods():
        if is_async:
            continue
        used = {
            child.attr if isinstance(child, ast.Attribute) else child.id
            for child in ast.walk(node)
            if isinstance(child, (ast.Attribute, ast.Name))
        }
        blocking = sorted(used & BLOCKING_NAMES)
        if blocking:
            offenders.append(f"{cls_name}.{node.name} (line {node.lineno}) uses {blocking}")

    assert not offenders, (
        "synchronous handler methods reaching a blocking Bifrost call: "
        f"{offenders}. Make the method `async def` and await it through "
        "`self._blocking(...)`, which runs it on the IOLoop's thread pool."
    )


def test_the_guard_would_catch_a_regression():
    """The guard above passes trivially if its rule never matches anything.

    Parse a synthetic handler with the defect and confirm the same rule flags
    it, so a rule that silently stopped matching cannot be mistaken for a clean
    codebase.
    """
    source = (
        "class BadHandler(_BifrostHandler):\n"
        "    def get(self):\n"
        "        client = client_from_env()\n"
        "        return client.list_clusters()\n"
    )
    tree = ast.parse(source)
    method = tree.body[0].body[0]
    assert isinstance(method, ast.FunctionDef), "the synthetic regression is not sync"
    used = {
        child.attr if isinstance(child, ast.Attribute) else child.id
        for child in ast.walk(method)
        if isinstance(child, (ast.Attribute, ast.Name))
    }
    assert used & BLOCKING_NAMES, "the shape rule no longer detects the defect it exists for"


def test_at_least_one_handler_is_async_so_the_scan_found_something():
    """Guards against the scan silently walking an empty set of methods."""
    methods = list(_handler_methods())
    assert methods, "no handler methods found — the AST scan is looking in the wrong place"
    assert any(is_async for _, _, is_async in methods)


@pytest.mark.parametrize("name", ["_apiclient.py", "handlers.py"])
def test_the_scanned_files_exist(name):
    assert (PACKAGE / name).is_file()
