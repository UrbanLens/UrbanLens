#!/usr/bin/env python3
"""Fail if the committed tree would import a module the committed tree lacks.

On 2026-08-13 a commit landed 139 files while leaving five modules untracked, and
19 of the committed files imported them. One - ``models/abstract/labelled.py`` -
sits on Django's model-loading path, so a fresh checkout raised
``ModuleNotFoundError`` while importing models: the web app, every management
command, both Celery workers and the whole test suite failed before doing
anything. Nothing caught it, because it is invisible from any working copy that
still has the files on disk, which is every machine the work was done on.

This checks the property directly: every ``urbanlens.*`` import in a tracked (or
newly staged) Python file must resolve to a file git will actually have. It is
deliberately structural rather than behavioural - importing the app to find out
would only prove the *working copy* is intact, which is exactly the thing that
was never in doubt.

Exits non-zero listing each offending import. Run by pre-commit; safe to run by
hand from the repo root.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

#: Import prefix that maps onto files in this repository.
_PACKAGE = "urbanlens"

#: Where that package's files live, relative to the repo root.
_PACKAGE_ROOT = pathlib.Path("src")

#: Template roots, in the order Django's app-directories loader would search.
_TEMPLATE_ROOTS = (
    pathlib.Path("src/urbanlens/dashboard/templates"),
    pathlib.Path("src/urbanlens/core/templates"),
    pathlib.Path("src/urbanlens/templates"),
)

#: Template paths Django resolves by convention, which never appear in source.
_CONVENTION_TEMPLATES = frozenset({"403.html", "404.html", "500.html"})


def _git(*args: str) -> set[str]:
    """Run a git command and return its stdout as a set of lines."""
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return {line for line in result.stdout.splitlines() if line}


def _visible_paths() -> set[str]:
    """Every path a fresh checkout of the resulting commit would contain.

    Tracked files plus anything newly staged, minus anything staged for deletion -
    the last matters because a commit that removes a module while leaving its
    importers behind fails identically.
    """
    tracked = _git("ls-files")
    added = _git("diff", "--cached", "--name-only", "--diff-filter=A")
    deleted = _git("diff", "--cached", "--name-only", "--diff-filter=D")
    return (tracked | added) - deleted


def _module_candidates(module: str) -> list[str]:
    """Paths that would satisfy ``import module`` - a module file or a package."""
    relative = module.replace(".", "/")
    return [
        str(_PACKAGE_ROOT / f"{relative}.py"),
        str(_PACKAGE_ROOT / relative / "__init__.py"),
    ]


def _imported_modules(source: str) -> set[str]:
    """Every ``urbanlens.*`` module named by an import in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name.startswith(f"{_PACKAGE}."))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith(f"{_PACKAGE}."):
            # `from a.b import c` - c may be a submodule or just a name, so both
            # the parent and the dotted child count as satisfying candidates.
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


#: Callables whose first string argument names a template.
_TEMPLATE_CALLS = frozenset({"render", "render_to_string", "get_template", "select_template", "TemplateResponse"})

#: Assignment targets whose string value names a template.
_TEMPLATE_NAMES = ("template_name", "_TEMPLATE", "_PARTIAL")


def _referenced_templates(source: str) -> set[str]:
    """Template paths this source actually renders.

    Restricted to literals passed to a known template callable or assigned to a
    template-shaped name, rather than every ``.html`` string in the file. A
    broader rule flags things like ``Takeout/My Activity/Maps/MyActivity.html`` -
    a path *inside a Google Takeout archive fixture* - and a hook that blocks
    commits cannot afford a false positive.

    Runtime-built names are deliberately not resolved; guessing them would fail
    commits over templates that are perfectly fine.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    found: set[str] = set()

    def literal(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(".html"):
            return node.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _TEMPLATE_CALLS:
                for arg in node.args[:2]:  # render(request, template) or render_to_string(template)
                    if (value := literal(arg)) is not None:
                        found.add(value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [getattr(t, "id", "") or getattr(t, "attr", "") for t in targets]
            if any(n == "template_name" or n.endswith(_TEMPLATE_NAMES) for n in names if n):
                if node.value is not None and (value := literal(node.value)) is not None:
                    found.add(value)
    return found


def _missing_templates(visible: set[str], python_files: list[str]) -> list[str]:
    """Templates referenced by committed Python that a fresh checkout would lack.

    Same failure as a missing module, one layer later: the app starts, then the
    view 500s with ``TemplateDoesNotExist`` the first time that path is rendered.
    """
    problems: list[str] = []
    for path in python_files:
        try:
            source = pathlib.Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for template in sorted(_referenced_templates(source)):
            if pathlib.Path(template).name in _CONVENTION_TEMPLATES:
                continue
            if any(str(root / template) in visible for root in _TEMPLATE_ROOTS):
                continue
            problems.append(f"{path}: renders {template}, which no committed file provides")
    return problems


def main() -> int:
    """Report imports that would not resolve in a fresh checkout."""
    visible = _visible_paths()
    python_files = sorted(path for path in visible if path.endswith(".py") and path.startswith(str(_PACKAGE_ROOT)))

    problems: list[str] = []
    for path in python_files:
        try:
            source = pathlib.Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for module in sorted(_imported_modules(source)):
            candidates = _module_candidates(module)
            if any(candidate in visible for candidate in candidates):
                continue
            # `from a.b import SomeClass` yields "a.b.SomeClass", which is a name
            # rather than a module. Only report it when its parent is missing too.
            parent = module.rsplit(".", 1)[0]
            if any(candidate in visible for candidate in _module_candidates(parent)):
                continue
            problems.append(f"{path}: imports {module}, which no committed file provides")

    problems.extend(_missing_templates(visible, python_files))

    if problems:
        sys.stderr.write("References a fresh checkout could not resolve:\n")
        for problem in sorted(set(problems)):
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write("\nThe module exists in your working copy but is not tracked or staged. `git add` it.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
