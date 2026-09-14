#!/usr/bin/env python3
"""Fail when a wiki resolved for *reading* is then written."""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

#: Services that mutate and save the wiki they are handed. Seeds a transitive closure: callers passing wiki on are writers too.
WRITER_SEEDS = frozenset({"apply_wiki_edit", "revert_wiki_edit", "revert_edit_fields", "save_edited_fields", "purge_recorded_value"})

#: Parameter names that hold a wiki. A function saving something it was handed
#: under another name is not something this can see; that is in the limits.
WIKI_PARAMS = frozenset({"wiki", "target"})

#: Kwargs receiving a wiki to read (possibly a projection), not to write.
READ_ONLY_KWARGS = frozenset({"baseline", "viewer"})

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
            # middle name is the one that might be a projection.
            if isinstance(target, ast.Tuple) and len(target.elts) == 3:
                element = target.elts[1]
            elif isinstance(target, ast.Name):
                element = target
            else:
                continue
            if isinstance(element, ast.Name) and not element.id.startswith("_"):
                names.add(element.id)
    return names


def _writes(fn: ast.FunctionDef, watched: set[str], writers: frozenset[str]) -> list[tuple[int, str]]:
    """Return (line, description) for each write to a watched name in *fn*."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"save", "delete"}:
            # `scope.wiki.save()` as well as `wiki.save()`. An attribute chain
            # was invisible before, which covered every one of the article
            # controller's resolve sites, since they all hold the wiki on a
            # dataclass.
            receiver = func.value
            root = receiver
            while isinstance(root, ast.Attribute):
                root = root.value
            names = set()
            if isinstance(receiver, ast.Name):
                names.add(receiver.id)
            if isinstance(root, ast.Name):
                names.add(root.id)
            hit = names & watched
            if hit:
                found.append((node.lineno, f"{sorted(hit)[0]}.{func.attr}()"))
                continue
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called in writers:
            # Keyword arguments count. `save_article_checked(wiki=wiki)` passes
            # the same value as `f(wiki)` does, and only positional args were
            # inspected until a review pointed at the gap.
            passed = [a for a in node.args if isinstance(a, ast.Name)]
            passed += [kw.value for kw in node.keywords if isinstance(kw.value, ast.Name) and kw.arg not in READ_ONLY_KWARGS]
            for arg in passed:
                if arg.id in watched:
                    found.append((node.lineno, f"{called}({arg.id}, ...)"))
    return found


def _transitive_writers(trees: dict[pathlib.Path, ast.Module]) -> frozenset[str]:
    """Grow :data:`WRITER_SEEDS` to a fixed point over the call graph.

    A function counts as a writer when it hands a wiki-holding parameter of its own to a function already known
    to be one.

    Args:
        trees: Parsed modules to search.

    Returns:
        Every function name that transitively writes a wiki it was given."""
    writers = set(WRITER_SEEDS)
    changed = True
    while changed:
        changed = False
        for tree in trees.values():
            for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                if fn.name in writers:
                    continue
                params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
                held = params & WIKI_PARAMS
                if not held:
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    called = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    if called not in writers:
                        continue
                    passed = {a.id for a in node.args if isinstance(a, ast.Name)}
                    passed |= {kw.value.id for kw in node.keywords if isinstance(kw.value, ast.Name)}
                    if passed & held:
                        writers.add(fn.name)
                        changed = True
                        break
    return frozenset(writers)


def main() -> int:
    """Report writes to a wiki that came back from the read-side resolve."""
    problems: list[str] = []
    sources: dict[pathlib.Path, str] = {}
    trees: dict[pathlib.Path, ast.Module] = {}
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            trees[path] = ast.parse(text)
        except SyntaxError:
            continue
        sources[path] = text

    writers = _transitive_writers(trees)

    for path, tree in trees.items():
        text = sources[path]
        if "resolve_visible_wiki" not in text and "self.resolve(" not in text:
            continue
        lines = text.splitlines()
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            watched = _resolved_names(fn)
            if not watched:
                continue
            # A name laundered through writable_wiki is no longer suspect; the
            # result is bound to a different name, which this never watches.
            for lineno, what in _writes(fn, watched, writers):
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
