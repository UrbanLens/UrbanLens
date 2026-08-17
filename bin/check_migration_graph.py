#!/usr/bin/env python3
"""Fail if a migration depends on one the committed tree won't have.

``makemigrations`` builds a new migration's ``dependencies`` from whatever files
are sitting in ``migrations/`` - it has no idea which of them git is tracking. So
an in-progress feature's uncommitted migration, left on disk in the same
checkout, can silently become the parent of a migration that *is* committed. The
result is invisible locally, because the parent is right there on disk, and fatal
everywhere else: any other checkout or deploy raises ``NodeNotFoundError`` on the
dangling dependency, before the app, the workers or the suite can start.

That is the same blind spot ``check_imports_tracked.py`` exists for, one level
over - the working copy is exactly the thing whose intactness was never in doubt.
It is checked structurally here rather than by loading Django, for the same
reason: importing the app would only prove this machine's files are complete.

Also refuses a dependency naming a migration that exists nowhere at all, which is
what a rename or a hand-edited graph leaves behind.

Exits non-zero listing each dangling dependency. Run by CI; safe to run by hand
from the repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

#: ``("app_label", "0007_name")`` inside a dependencies list.
_DEPENDENCY = re.compile(r"""\(\s*['"]([\w.]+)['"]\s*,\s*['"](\w+)['"]\s*\)""")

#: The ``dependencies = [...]`` assignment itself. Non-greedy so a later
#: ``operations = [...]`` in the same file can't be swallowed into it.
_DEPENDENCIES_BLOCK = re.compile(r"dependencies\s*=\s*\[(.*?)\]", re.DOTALL)


def _tracked_paths() -> set[str]:
    """Every path git will hand a fresh checkout."""
    listing = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return set(listing.stdout.split())


def check() -> int:
    """Report migrations whose in-app dependencies are missing or untracked.

    Returns:
        Process exit code: non-zero when any dependency would dangle in a fresh
        checkout.
    """
    tracked = _tracked_paths()
    problems: list[str] = []
    scanned = 0

    directories = sorted({path.parent for path in pathlib.Path("src").rglob("migrations/*.py")})
    for directory in directories:
        app_label = directory.parent.name
        on_disk = {path.stem for path in directory.glob("*.py") if path.name != "__init__.py"}
        committed = {path.stem for path in directory.glob("*.py") if path.name != "__init__.py" and str(path) in tracked}

        for stray in sorted(on_disk - committed):
            problems.append(f"{directory}/{stray}.py is not tracked by git - commit it or remove it before it becomes someone's dependency")

        for path in sorted(directory.glob("*.py")):
            if path.name == "__init__.py":
                continue
            scanned += 1
            block = _DEPENDENCIES_BLOCK.search(path.read_text())
            if block is None:
                continue
            for dependency_app, dependency_name in _DEPENDENCY.findall(block.group(1)):
                # Cross-app dependencies point at Django's own or a third party's
                # migrations, which are installed rather than committed here.
                if dependency_app != app_label:
                    continue
                if dependency_name not in on_disk:
                    problems.append(f"{path}: depends on {dependency_app}.{dependency_name}, which exists nowhere on disk")
                elif dependency_name not in committed:
                    problems.append(f"{path}: depends on {dependency_app}.{dependency_name}, which git is NOT tracking - a fresh checkout raises NodeNotFoundError")

    if problems:
        print(f"Migration graph problems ({len(problems)}):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"All {scanned} migrations depend only on migrations the committed tree has.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
