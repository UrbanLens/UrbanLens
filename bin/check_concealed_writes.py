#!/usr/bin/env python3
"""Fail when a wiki resolved for *reading* is then written.

``services.wiki.wiki_access.resolve_visible_wiki`` is the single gate every
wiki-scoped surface passes through - 99 call sites, including all 31 external
API handlers. Since concealment moved to resolve time, what it hands back may
be a *projection*: a real Wiki instance carrying only the field values this
viewer is entitled to see.

Reading one is the point. Saving one is a data-loss bug: it persists one
viewer's redacted view over what the community actually wrote. The projection
refuses ``save()`` for that reason, but a refusal is a 500 - and a 500 only
gated accounts receive is precisely the tell the whole feature exists to avoid.
Write paths call ``concealment.writable_wiki`` and act on what it returns.

Nine such paths existed when this check was written, spread over four modules;
all nine looked correct, because the projection is a Wiki and mutating one is
ordinary Django. Nothing about the call site says which kind of row it holds.

The heuristic: in any function that binds a name from ``resolve_visible_wiki``
or ``self.resolve``, flag that name being saved, deleted, or passed to a known
wiki-writing service.

Known limits, so a pass is not read as more than it is:
  - it follows names, not values, so re-binding through a helper hides the write;
  - ``WRITERS`` is a list, and a new wiki-writing service has to be added to it;
  - it only sees writes in the same function as the resolve.
A pass means "no *detected* write", not "no write".

Mark a deliberate case with ``concealed-write-ok: <why>`` on or above the line.

Exits non-zero listing each write.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

#: Services that mutate and save the wiki they are handed.
WRITERS = frozenset({"apply_wiki_edit", "revert_wiki_edit", "revert_edit_fields", "save_edited_fields", "purge_recorded_value"})

#: The call that converts a possible projection into a row safe to write.
LAUNDER = "writable_wiki"

EXEMPT_MARKER = "concealed-write-ok:"


def _resolved_names(fn: ast.FunctionDef) -> set[str]:
    """Return names in *fn* bound to the result of a wiki resolve call."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called not in {"resolve_visible_wiki", "resolve"}:
            continue
        for target in node.targets:
            # Both resolvers return (location, wiki, profile), so only the
            # middle name is the one that might be a projection. Watching all
            # three flagged `location` and `profile` being passed to services
            # that take them alongside the wiki - noise that would have taught
            # a reader to add exemptions rather than fix writes.
            if isinstance(target, ast.Tuple) and len(target.elts) == 3:
                element = target.elts[1]
            elif isinstance(target, ast.Name):
                element = target
            else:
                continue
            if isinstance(element, ast.Name) and not element.id.startswith("_"):
                names.add(element.id)
    return names


def _writes(fn: ast.FunctionDef, watched: set[str]) -> list[tuple[int, str]]:
    """Return (line, description) for each write to a watched name in *fn*."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"save", "delete"} and isinstance(func.value, ast.Name) and func.value.id in watched:
            found.append((node.lineno, f"{func.value.id}.{func.attr}()"))
            continue
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called in WRITERS:
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in watched:
                    found.append((node.lineno, f"{called}({arg.id}, ...)"))
    return found


def main() -> int:
    """Report writes to a wiki that came back from the read-side resolve."""
    problems: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "resolve_visible_wiki" not in text and "self.resolve(" not in text:
            continue
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            watched = _resolved_names(fn)
            if not watched:
                continue
            # A name laundered through writable_wiki is no longer suspect; the
            # result is bound to a different name, which this never watches.
            for lineno, what in _writes(fn, watched):
                context = "\n".join(lines[max(0, lineno - 3) : lineno])
                if EXEMPT_MARKER in context:
                    continue
                rel = path.relative_to(REPO_ROOT)
                problems.append(f"{rel}:{lineno}: {what} - may be a concealed projection")

    if problems:
        sys.stderr.write("Writes to a wiki resolved for reading:\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write(f"\nPass it through concealment.{LAUNDER}() first, or mark it '{EXEMPT_MARKER} <why>'.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
