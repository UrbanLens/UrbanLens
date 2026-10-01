#!/usr/bin/env python3
"""Rank models by how many distinct modules write them, and flag whole-row saves."""

from __future__ import annotations

import ast
import collections
import pathlib
import re
import sys

#: Assignments to these on a model instance are writes, not reads.
_WRITE_CALLS = {"save", "update", "bulk_update", "update_or_create", "get_or_create", "create", "bulk_create"}

#: Directories whose writes don't indicate shared ownership: tests set up any
#: model freely, and migrations write everything by definition.
_SKIP = ("/tests/", "/migrations/")

#: Default threshold.
_DEFAULT_MIN_WRITERS = 3


def _model_names() -> set[str]:
    """Every Django model class defined in the project.

    Returns:
        Class names of models, used to attribute writes to a model."""
    names: set[str] = set()
    for path in pathlib.Path("src").rglob("models/**/*.py"):
        if any(skip in path.as_posix() for skip in _SKIP):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any("Model" in ast.unparse(base) for base in node.bases):
                names.add(node.name)
    return names


def _freshly_constructed(tree: ast.AST, models: set[str]) -> set[tuple[int, str]]:
    """Locals that hold an instance this function just built, per function.

    A ``save()`` on one of those is an INSERT, not a whole-row overwrite of a row somebody else may have changed
    - there is no earlier load to be stale relative to.

    Args:
        tree: The module's parsed AST.
        models: Known model class names.

    Returns:
        ``(function id, local name)`` pairs safe to skip."""
    fresh: set[tuple[int, str]] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        constructed: set[str] = set()
        rebound: set[str] = set()
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            name = node.targets[0].id
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in models:
                constructed.add(name)
            else:
                rebound.add(name)
        fresh.update((id(function), name) for name in constructed - rebound)
    return fresh


def _enclosing_function(tree: ast.AST) -> dict[int, int]:
    """Map every node to the id of the function that contains it.

    Args:
        tree: The module's parsed AST.

    Returns:
        ``{id(node): id(function)}`` for nodes inside a function."""
    owner: dict[int, int] = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(function):
                owner.setdefault(id(node), id(function))
    return owner


def _instance_hint(node: ast.Call, models: set[str]) -> str | None:
    """Guess which model a write targets.

    Uses the receiver's spelling: ``Pin.objects.filter(...).update(...)`` names its model outright, and
    ``pin.save()`` names it by convention.

    Args:
        node: The call being attributed.
        models: Known model class names.

    Returns:
        A model name, or None when the receiver doesn't identify one."""
    source = ast.unparse(node)
    for model in models:
        if re.search(rf"\b{re.escape(model)}\.objects\b", source):
            return model
    receiver = source.split(".")[0]
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", receiver).lower().lstrip("_")
    # Exact before suffix, or `profile.save()` attributes to ConsensusProfile as
    # readily as to Profile - the suffix of a compound name matches a bare one.
    for model in models:
        if snake == re.sub(r"(?<!^)(?=[A-Z])", "_", model).lower():
            return model
    for model in models:
        if snake == re.sub(r"(?<!^)(?=[A-Z])", "_", model).lower().split("_")[-1]:
            return model
    return None


def main(argv: list[str]) -> int:
    """Print the writer-cardinality report.

    Args:
        argv: Command-line arguments.

    Returns:
        Always 0 - this reports, it does not gate."""
    min_writers = _DEFAULT_MIN_WRITERS
    if "--min-writers" in argv:
        min_writers = int(argv[argv.index("--min-writers") + 1])
    show_all = "--all" in argv

    models = _model_names()
    writers: dict[str, set[str]] = collections.defaultdict(set)
    bare_saves: dict[str, list[str]] = collections.defaultdict(list)

    for path in pathlib.Path("src").rglob("*.py"):
        if any(skip in path.as_posix() for skip in _SKIP):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        fresh = _freshly_constructed(tree, models)
        owner = _enclosing_function(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _WRITE_CALLS:
                continue
            model = _instance_hint(node, models)
            if model is None:
                continue
            writers[model].add(str(path))
            if node.func.attr != "save" or node.args or node.keywords:
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and (owner.get(id(node), 0), receiver.id) in fresh:
                continue
            bare_saves[model].append(f"{path}:{node.lineno}")

    ranked = sorted(writers.items(), key=lambda item: len(item[1]), reverse=True)
    print(f"{'model':32} {'writers':>8}  {'bare save()':>11}")
    print("-" * 56)
    flagged = 0
    for model, modules in ranked:
        if len(modules) < min_writers and not show_all:
            continue
        bare = bare_saves.get(model, [])
        marker = "  <-- whole-row writes on a contested row" if bare and len(modules) >= min_writers else ""
        print(f"{model:32} {len(modules):>8}  {len(bare):>11}{marker}")
        if bare:
            flagged += 1
            for site in bare[:5]:
                print(f"      {site}")

    print()
    print(f"{len(ranked)} models written outside tests/migrations; {flagged} of those shown carry a bare save().")
    print("A bare save() writes every column from a possibly-stale instance. On a row with")
    print("several writers that is a lost update waiting for concurrency - check each one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
