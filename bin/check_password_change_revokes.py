#!/usr/bin/env python3
"""Fail if production code sets a password without deciding what the change revokes.

``services.auth.credential_revocation.revoke_credentials_on_password_change`` ends the OAuth2 grants (and, on
request, the API keys) that should not outlive a password, and keeps the requester's session. A function that
calls ``set_password``/``set_unusable_password`` must call it too, or say why not next to the call.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

_SETTERS = frozenset({"set_password", "set_unusable_password"})
_HELPER = "revoke_credentials_on_password_change"

#: Marker for a password write that deliberately revokes nothing - provisioning a fixture account, say. Spelled
#: out beside the call so the exemption is visible where the decision is.
_ALLOW_MARKER = "password-change-ok:"


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_calls(function: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    """Calls to ``names`` in ``function``'s own body, not in functions nested inside it."""
    found = []
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, _FUNCTIONS):
            continue
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in names:
                found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def _offences_in_file(path: pathlib.Path, tree: ast.AST, lines: list[str]) -> list[str]:
    offences = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        setters = _own_calls(function, _SETTERS)
        if not setters or _own_calls(function, frozenset({_HELPER})):
            continue
        for call in setters:
            context = "\n".join(lines[max(0, call.lineno - 2) : call.lineno])
            if _ALLOW_MARKER in context:
                continue
            offences.append(
                f"{path.relative_to(REPO_ROOT)}:{call.lineno}: {function.name}() changes a password without {_HELPER}(). Call it, or mark the line `{_ALLOW_MARKER} <reason>`.",
            )
    return offences


def main() -> int:
    """Report every production password write that skips the revocation decision."""
    offences: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        posix = path.as_posix()
        if "/tests/" in posix or "/migrations/" in posix:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(f"{name}(" in text for name in _SETTERS):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        offences.extend(_offences_in_file(path, tree, text.split("\n")))

    if offences:
        print(f"Password changes that skip the revocation decision ({len(offences)}):")
        for offence in offences:
            print(f"  {offence}")
        return 1
    print("Every password change decides what it revokes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
