#!/usr/bin/env python3
"""Fail when a post_save subscription watches a field only ever set by update().

Three separate rules in the reputation ledger subscribed to ``post_save`` on a
model whose real transition happens through ``QuerySet.update()``, which emits
no signal at all - and emits none *deliberately*, because those transitions are
atomic compare-and-sets:

    FriendInvitation.mark_accepted  -> .update(accepted_at=now)
    WikiManager.claim_for_location  -> .update(officially_created=True, ...)
    revert_wiki_edit                -> .update(reverted=False, reverted_by=None)

All three looked right in review. All three could never fire. That is the same
shape as the defect ``check_notification_choke_point.py`` exists for: a rule
kept in one place, and a write path that goes around it.

The heuristic: for every model named as a signal sender, collect the field names
the subscription's predicate mentions, then look for ``.update(field=...)`` on
that same model anywhere in production code. A match means the write the
subscription is waiting for happens somewhere the subscription cannot see.

Known limits, stated so nobody trusts a pass further than it deserves:
  - it matches models and fields by name, so two models sharing a field name can
    produce a false positive;
  - it only sees ``Model.objects...update(...)`` and ``Model.objects...filter(
    ...).update(...)`` spelled with the model's own name;
  - it cannot see an update built through a variable or a related manager.
A pass means "no *detected* gap", not "the subscription fires".

Mark a deliberate case with ``signal-update-ok: <ModelName> <why>`` in a comment
- the model is named in the marker so the exemption stays legible when lines
move, and it sits next to the decision the way the notification check's does.

Exits non-zero listing each unreachable-looking subscription.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

_ALLOW_MARKER = "signal-update-ok:"


def _iter_python_files() -> list[pathlib.Path]:
    """Return production Python files, excluding tests and migrations."""
    return [p for p in sorted(SEARCH_ROOT.rglob("*.py")) if "/tests/" not in p.as_posix() and "/migrations/" not in p.as_posix()]


def _subscribed_models(tree: ast.AST) -> dict[str, set[str]]:
    """Return ``{model_name: {field names its predicate mentions}}``.

    Reads both spellings this codebase uses: a declarative ``_Subscription``
    table, and a direct ``post_save.connect(..., sender=Model)``.
    """
    found: dict[str, set[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")

        if name == "_Subscription":
            model_name = ""
            fields: set[str] = set()
            for keyword in node.keywords:
                if keyword.arg == "model_path" and isinstance(keyword.value, ast.Constant):
                    model_name = str(keyword.value.value).rsplit(":", 1)[-1]
                elif keyword.arg in {"qualifies", "profile_id", "wiki_id"}:
                    fields |= {sub.attr for sub in ast.walk(keyword.value) if isinstance(sub, ast.Attribute)}
            if model_name:
                found.setdefault(model_name, set()).update(fields)

        elif name == "connect":
            for keyword in node.keywords:
                if keyword.arg == "sender" and isinstance(keyword.value, ast.Name):
                    found.setdefault(keyword.value.id, set())

    return found


def _updated_fields_by_model(tree: ast.AST) -> dict[str, set[str]]:
    """Return ``{model_name: {field names passed to .update()}}``."""
    found: dict[str, set[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "update":
            continue

        # Walk back down the chain to whatever the expression started from.
        # Every branch must make progress or this loop does not terminate - a
        # Call whose func is a bare Name has nowhere further to go, so it stops
        # there rather than re-testing the same node forever.
        root: ast.expr = node.func.value
        while True:
            if isinstance(root, ast.Attribute):
                root = root.value
            elif isinstance(root, ast.Call) and isinstance(root.func, ast.Attribute):
                root = root.func.value
            else:
                break
        if not isinstance(root, ast.Name):
            continue

        fields = {keyword.arg for keyword in node.keywords if keyword.arg}
        if fields:
            found.setdefault(root.id, set()).update(fields)

    return found


def main() -> int:
    """Report every subscription whose awaited field is written by update()."""
    subscriptions: dict[str, set[str]] = {}
    updates: dict[str, set[str]] = {}
    exempt_models: set[str] = set()

    for path in _iter_python_files():
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue

        for line in source.splitlines():
            if _ALLOW_MARKER in line:
                # The model is named in the marker itself, so an exemption is
                # legible without cross-referencing a line number that moves
                # the next time somebody edits above it.
                exempt_models.update(word.strip(",.:;()") for word in line.split(_ALLOW_MARKER, 1)[1].split())

        for model, fields in _subscribed_models(tree).items():
            subscriptions.setdefault(model, set()).update(fields)
        for model, fields in _updated_fields_by_model(tree).items():
            updates.setdefault(model, set()).update(fields)

    problems: list[str] = []
    for model, watched in sorted(subscriptions.items()):
        if model in exempt_models:
            continue
        written = updates.get(model, set())
        collisions = sorted(watched & written)
        if collisions:
            problems.append(f"{model}: subscribed on post_save, but {', '.join(collisions)} is set via queryset .update() - the signal cannot see that write")

    if problems:
        sys.stderr.write("Signal subscriptions that a queryset update() bypasses:\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write("\nRecord the event at the transition, or mark it 'signal-update-ok: <why>'.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
