#!/usr/bin/env python3
"""Fail if a panel/media ``fetch`` can cache a result after swallowing an error."""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

#: Functions whose job is "get data from a source and store it". Named rather
#: than inferred: a function that caches is not automatically suspect - one that
#: caches *what it just fetched* is. ``fetch`` alone missed
#: ``get_satellite_slides``, which cached an empty carousel after its provider
#: failed, so the list grew when that surfaced rather than staying a guess.
_CHECKED_FUNCTIONS = frozenset({"fetch", "get_satellite_slides", "get_street_view_slides"})

#: Calls that persist a fetch's result. Matched on attribute name so a caller
#: aliasing the model still trips it.
_PERSISTING_CALLS = {"set", "set_many"}
_PERSISTING_TARGETS = {"LocationCache", "cache"}

#: Calls that hand a value straight back rather than storing it - a `cache.get`
#: inside a handler is not a write and must not be mistaken for one.
_READ_ONLY_CALLS = {"get", "get_many"}


#: Marker for a handler that deliberately falls through to a write, e.g. one
#: provider of several failed and the partial result is genuinely worth caching.
#: Spelled out in the source next to the handler, so the exemption is visible
#: where the decision is - an exemption nobody re-reads is how a check rots.
_ALLOW_MARKER = "outage-cache-ok:"


def _handler_exits(handler: ast.ExceptHandler) -> bool:
    """Whether an except handler always leaves the function or re-raises."""
    return any(isinstance(node, (ast.Raise, ast.Return)) for node in ast.walk(handler))


def _is_allowed(handler: ast.ExceptHandler, source_lines: list[str]) -> bool:
    """Whether this handler carries an explicit, explained exemption marker."""
    start = handler.lineno - 1
    end = min(handler.end_lineno or handler.lineno, len(source_lines))
    return any(_ALLOW_MARKER in source_lines[index] for index in range(start, end))


def _is_persisting_call(node: ast.AST) -> bool:
    """Whether a node is a call that writes a fetch result to a store."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr in _READ_ONLY_CALLS or node.func.attr not in _PERSISTING_CALLS:
        return False
    target = node.func.value
    return isinstance(target, ast.Name) and target.id in _PERSISTING_TARGETS


def _offences_in_function(func: ast.FunctionDef, path: pathlib.Path, source_lines: list[str]) -> list[str]:
    """Every swallowing handler in ``func`` followed by a persisting call."""
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        swallowing = [h for h in node.handlers if not _handler_exits(h) and not _is_allowed(h, source_lines)]
        if not swallowing:
            continue
        # A persisting call anywhere after the try, in the same function.
        later_writes = [n for n in ast.walk(func) if _is_persisting_call(n) and getattr(n, "lineno", 0) > node.lineno]
        if later_writes:
            found.append(
                f"{path.relative_to(REPO_ROOT)}:{swallowing[0].lineno}: `{func.name}` swallows an exception here and still caches at line {later_writes[0].lineno} - an outage would be stored as a result. Return without writing, or re-raise.",
            )
    return found


def main() -> int:
    """Report every fetch that can cache a swallowed failure."""
    offences: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        source_lines = text.split("\n")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in _CHECKED_FUNCTIONS:
                offences.extend(_offences_in_function(node, path, source_lines))

    if offences:
        print(f"Fetches that can cache an outage as a result ({len(offences)}):")
        for offence in offences:
            print(f"  {offence}")
        return 1
    print("No fetch caches a swallowed failure.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
