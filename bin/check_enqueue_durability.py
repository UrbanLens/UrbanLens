#!/usr/bin/env python3
"""Fail if a caller inspects ``safely_enqueue_task``'s result without choosing ``durable=``.

A durable enqueue the broker refuses is kept in the task outbox and queued later. A caller that also handles
the ``None`` itself (reports the failure, runs the work inline, releases a claim) would then do the work
twice, or tell the user it failed when it will in fact run. Using the result is the sign a caller has its own
plan, so it must say whether the outbox applies.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from typing import TypeGuard

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"
_FUNCTION = "safely_enqueue_task"


def _is_enqueue(node: ast.AST) -> TypeGuard[ast.Call]:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == _FUNCTION) or (isinstance(func, ast.Attribute) and func.attr == _FUNCTION)


def _result_discarded(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    # A lambda handed to on_commit returns into nothing.
    return isinstance(parent, (ast.Expr, ast.Lambda))


def offences_in(path: pathlib.Path) -> list[str]:
    """Every call in ``path`` that uses the result without naming ``durable``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = []
    for node in ast.walk(tree):
        if not _is_enqueue(node) or _result_discarded(node, parents):
            continue
        if any(keyword.arg == "durable" for keyword in node.keywords):
            continue
        found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: the result of {_FUNCTION} is used, so pass durable=False if this code handles a refused enqueue itself, or durable=True if the outbox should retry it.")
    return found


def main() -> int:
    """Report every enqueue whose caller uses the result without choosing durability."""
    offences: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        posix = path.as_posix()
        if "/tests/" in posix or "/migrations/" in posix:
            continue
        offences.extend(offences_in(path))
    if offences:
        print(f"Enqueues that use the result without choosing durable= ({len(offences)}):")
        for offence in offences:
            print(f"  {offence}")
        return 1
    print("Every enqueue whose result is used chooses durable=.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
