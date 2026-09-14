#!/usr/bin/env python3
"""Fail if production code raises a notification without going through the choke point."""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

_MODEL = "NotificationLog"

#: Manager calls that write a row without consulting any delivery preference.
_BYPASSING_CALLS = frozenset({"create", "get_or_create", "update_or_create", "bulk_create", "acreate"})

#: Marker for a write that deliberately bypasses the preference - a repair
#: script, a re-delivery of a row that already passed the check. Spelled out in
#: the source next to the call, so the exemption is visible where the decision
#: is; an exemption nobody re-reads is how a check rots.
_ALLOW_MARKER = "notify-bypass-ok:"


def _is_notification_manager(node: ast.AST) -> bool:
    """True for ``NotificationLog.objects`` (however the manager is spelled)."""
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == _MODEL


def _offences_in_file(path: pathlib.Path, tree: ast.AST, lines: list[str]) -> list[str]:
    """Report every bypassing write in one module."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        target = ""
        if isinstance(node.func, ast.Attribute) and node.func.attr in _BYPASSING_CALLS and _is_notification_manager(node.func.value):
            target = f"{_MODEL}.objects.{node.func.attr}()"
        elif isinstance(node.func, ast.Name) and node.func.id == _MODEL:
            target = f"{_MODEL}(...)"
        if not target:
            continue

        # The marker may sit on the call's own line or the line above it.
        context = "\n".join(lines[max(0, node.lineno - 2) : node.lineno])
        if _ALLOW_MARKER in context:
            continue

        found.append(
            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target} bypasses the mute preference. Use NotificationLog.objects.notify(), or mark the line `{_ALLOW_MARKER} <reason>`.",
        )
    return found


def main() -> int:
    """Report every production notification write that skips ``notify()``."""
    offences: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        if _MODEL not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        offences.extend(_offences_in_file(path, tree, text.split("\n")))

    if offences:
        print(f"Notification writes that bypass the mute preference ({len(offences)}):")
        for offence in offences:
            print(f"  {offence}")
        return 1
    print("Every notification write goes through NotificationLog.objects.notify().")
    return 0


if __name__ == "__main__":
    sys.exit(main())
