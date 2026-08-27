#!/usr/bin/env python3
"""Fail if a model opts into field versioning without the machinery that records it.

Provenance has to be recorded at write time. Inferring it afterwards from edit
history does not work here, and the reason is instructive: three writers
already bypass the wiki's existing history entirely - a bulk ``update()`` in
``tasks.py``, a bare ``save()`` in ``wiki_creation.py``, and one in
``markup.py`` that omits ``updated`` from ``update_fields``. None of the three
is visible to a ``post_save`` receiver.

So the recording is interception rather than a funnel callers must remember:
``VersionedModel.save()`` and ``VersionedQuerySet.update()``/``bulk_update()``.
This check is what stops a model half-adopting it. Three ways to end up with
silent gaps, all of which look fine in review:

1. ``versioned_fields`` declared on a model that does not inherit
   ``VersionedModel`` - nothing records anything, and the field list reads as
   though something does.
2. ``VersionedModel`` inherited but the manager's queryset is not a
   ``VersionedQuerySet`` - instance saves are recorded and every ``update()``
   is not, which is the worse half.
3. ``versioned_fields`` naming a field the model does not have - a rename that
   silently stopped versioning a column.

Exits non-zero listing each problem. Safe to run by hand from the repo root.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

_MIXIN = "VersionedModel"
_QUERYSET = "VersionedQuerySet"


def _base_names(node: ast.ClassDef) -> set[str]:
    """Return the class's base names, however they were written.

    Handles both ``VersionedModel`` and ``abstract.VersionedModel``; this
    codebase uses the dotted form for model bases and the bare form inside the
    abstract package itself.
    """
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _assigned_names(node: ast.ClassDef, target: str) -> ast.expr | None:
    """Return the value assigned to ``target`` in the class body, if any."""
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            for name in statement.targets:
                if isinstance(name, ast.Name) and name.id == target:
                    return statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == target:
            return statement.value
    return None


def _declared_fields(node: ast.ClassDef) -> set[str]:
    """Return the field names assigned in the class body."""
    fields: set[str] = set()
    for statement in node.body:
        if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
            continue
        for name in statement.targets:
            if isinstance(name, ast.Name):
                fields.add(name.id)
    return fields


def _versioned_field_names(value: ast.expr | None) -> list[str]:
    """Return the literal strings in a ``versioned_fields`` declaration."""
    if not isinstance(value, ast.Tuple | ast.List):
        return []
    return [element.value for element in value.elts if isinstance(element, ast.Constant) and isinstance(element.value, str)]


def main() -> int:
    """Check every model class, reporting each half-adopted versioning setup."""
    problems: list[str] = []
    versioned_models: dict[str, pathlib.Path] = {}

    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = _base_names(node)
            declaration = _assigned_names(node, "versioned_fields")
            inherits = _MIXIN in bases

            if node.name == _MIXIN:
                # The mixin itself declares the empty default. Exempting it by
                # name rather than by "is abstract" keeps the check from
                # needing to understand Django's Meta.
                continue

            if declaration is not None and not inherits:
                problems.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.name} declares versioned_fields but does not inherit {_MIXIN}")
            if inherits and declaration is None:
                problems.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.name} inherits {_MIXIN} but declares no versioned_fields")

            if inherits and declaration is not None:
                versioned_models[node.name] = path
                if _assigned_names(node, "revision_model") is None:
                    problems.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.name} is versioned but names no revision_model")
                declared = _declared_fields(node)
                # Inherited fields (security indicators, addressable columns)
                # are not in this class body, so only flag a name that matches
                # nothing anywhere - a bare miss is usually a rename.
                for field_name in _versioned_field_names(declaration):
                    if declared and field_name not in declared and not _field_exists_anywhere(field_name):
                        problems.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.name}.versioned_fields names '{field_name}', which no model declares")

    problems.extend(_queryset_problems(versioned_models))

    if problems:
        sys.stderr.write("Versioned-write problems:\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        return 1
    return 0


_ALL_FIELD_NAMES: set[str] | None = None


def _field_exists_anywhere(field_name: str) -> bool:
    """Whether any model in the tree declares a field by this name.

    Deliberately loose. A versioned model inherits fields from several abstract
    bases, so checking only its own body would flag every inherited column;
    checking the whole tree still catches the case that matters, which is a
    rename leaving a name nothing answers to.
    """
    global _ALL_FIELD_NAMES  # noqa: PLW0603
    if _ALL_FIELD_NAMES is None:
        _ALL_FIELD_NAMES = set()
        for path in SEARCH_ROOT.rglob("*.py"):
            if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    _ALL_FIELD_NAMES |= _declared_fields(node)
    return field_name in _ALL_FIELD_NAMES


def _queryset_problems(versioned_models: dict[str, pathlib.Path]) -> list[str]:
    """Report versioned models whose queryset does not intercept bulk writes.

    This is the half that matters most: ``update()`` skips ``save()`` and every
    signal, so a model with the mixin but a plain queryset records instance
    saves and silently drops every bulk write.
    """
    problems: list[str] = []
    for model_name, model_path in sorted(versioned_models.items()):
        queryset_path = model_path.parent / "queryset.py"
        if not queryset_path.exists():
            problems.append(f"{model_path.relative_to(REPO_ROOT)}: {model_name} is versioned but has no queryset.py to intercept bulk writes")
            continue
        if _QUERYSET not in queryset_path.read_text(encoding="utf-8"):
            problems.append(f"{queryset_path.relative_to(REPO_ROOT)}: {model_name} is versioned but its queryset does not inherit {_QUERYSET}; update() would not be recorded")
    return problems


if __name__ == "__main__":
    sys.exit(main())
