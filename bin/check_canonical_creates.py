#!/usr/bin/env python3
"""Fail if a Location, Label or alias is get-or-created by hand instead of through its canonical helper.

Each table carries a unique constraint a plain ``get_or_create`` cannot match: Location's
``(latitude, longitude)`` is numeric(9,6), so a raw float's lookup misses the rounded row and the insert
collides; Label's and the aliases' are on ``lower(name)``, and an alias's ``save()`` also sanitizes the name,
so a raw-name lookup misses the stored row. The helpers quantize, sanitize and match case-insensitively, and
absorb a concurrent insert.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

_ALL_CREATES = frozenset({"create", "get_or_create", "update_or_create"})
_GET_OR_CREATES = frozenset({"get_or_create", "update_or_create"})

#: Model name -> (the manager methods refused, the helpers to use instead). An alias's plain ``create`` stays
#: allowed: the paths that use it refuse a duplicate inside their own savepoint.
_CANONICAL: dict[str, tuple[frozenset[str], str]] = {
    "Location": (_ALL_CREATES, "Location.objects.get_exact_or_create / get_nearby_or_create, or services.visits.resolve_location_for_point"),
    "Label": (_ALL_CREATES, "Label.objects.resolve_or_create (reuse) or Label.objects.create_unique (refuse on conflict)"),
    "PinAlias": (_GET_OR_CREATES, "PinAlias.objects.resolve_or_create"),
    "WikiAlias": (_GET_OR_CREATES, "WikiAlias.objects.resolve_or_create"),
}

#: A deliberate exception, explained on the same line or the line above - e.g. an undo restore that has
#: already refused the conflict and catches the constraint itself.
_ALLOW_MARKER = "canonical-create-ok:"


def _offending_call(node: ast.Call) -> tuple[str, str] | None:
    """``(model, method)`` when *node* is ``<Model>.objects.<creating method>(...)`` for a guarded model."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    manager = func.value
    if not isinstance(manager, ast.Attribute) or manager.attr != "objects":
        return None
    model = manager.value
    if isinstance(model, ast.Name) and model.id in _CANONICAL and func.attr in _CANONICAL[model.id][0]:
        return model.id, func.attr
    return None


def offences(source: str, display_path: str) -> list[str]:
    """Every unmarked hand-written create in *source*.

    Args:
        source: A Python module's text.
        display_path: The path to name in each message.

    Returns:
        One message per offence.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or (call := _offending_call(node)) is None:
            continue
        model, method = call
        nearby = lines[max(node.lineno - 2, 0) : node.lineno]
        if any(_ALLOW_MARKER in line for line in nearby):
            continue
        found.append(f"{display_path}:{node.lineno}: {model}.objects.{method}() - use {_CANONICAL[model][1]}")
    return found


def main() -> int:
    """Scan the application source. Returns the process exit code."""
    failures: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        posix = path.as_posix()
        if "/tests/" in posix or "/migrations/" in posix:
            continue
        failures.extend(offences(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT))))
    if failures:
        print("\n".join(failures))
        print(f"\n{len(failures)} hand-written create(s). Mark a deliberate one with `# {_ALLOW_MARKER} <why>`.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
