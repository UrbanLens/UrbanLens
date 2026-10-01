#!/usr/bin/env python3
"""Fail if a migration depends on one the committed tree won't have."""

from __future__ import annotations

import pathlib
import re
import subprocess

#: ``("app_label", "0007_name")`` inside a dependencies list. The optional
#: trailing comma matters: formatters split long tuples across lines and leave
#: one, and without it this silently matched nothing for those - which hid every
#: dependency in a reformatted migration, including the ones that make a
#: migration a non-leaf.
_DEPENDENCY = re.compile(r"""\(\s*['"]([\w.]+)['"]\s*,\s*['"](\w+)['"]\s*,?\s*\)""")

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
        Process exit code: non-zero when any dependency would dangle in a fresh checkout."""
    tracked = _tracked_paths()
    problems: list[str] = []
    scanned = 0
    #: app label -> {migration name: names it depends on within the same app}
    graph: dict[str, dict[str, set[str]]] = {}

    directories = sorted({path.parent for path in pathlib.Path("src").rglob("migrations/*.py")})
    for directory in directories:
        app_label = directory.parent.name
        on_disk = {path.stem for path in directory.glob("*.py") if path.name != "__init__.py"}
        committed = {path.stem for path in directory.glob("*.py") if path.name != "__init__.py" and path.as_posix() in tracked}

        for stray in sorted(on_disk - committed):
            problems.append(f"{directory}/{stray}.py is not tracked by git - commit it or remove it before it becomes someone's dependency")

        for path in sorted(directory.glob("*.py")):
            if path.name == "__init__.py":
                continue
            scanned += 1
            block = _DEPENDENCIES_BLOCK.search(path.read_text(encoding="utf-8"))
            if block is None:
                continue
            graph.setdefault(app_label, {}).setdefault(path.stem, set())
            for dependency_app, dependency_name in _DEPENDENCY.findall(block.group(1)):
                # Cross-app dependencies point at Django's own or a third party's
                # migrations, which are installed rather than committed here.
                if dependency_app != app_label:
                    continue
                graph[app_label][path.stem].add(dependency_name)
                if dependency_name not in on_disk:
                    problems.append(f"{path}: depends on {dependency_app}.{dependency_name}, which exists nowhere on disk")
                elif dependency_name not in committed:
                    problems.append(f"{path}: depends on {dependency_app}.{dependency_name}, which git is NOT tracking - a fresh checkout raises NodeNotFoundError")

    for app_label, migrations in graph.items():
        depended_on = {name for parents in migrations.values() for name in parents}
        leaves = sorted(set(migrations) - depended_on)
        if len(leaves) > 1:
            problems.append(f"{app_label}: the migration graph has {len(leaves)} leaves ({', '.join(leaves)}) - Django refuses to migrate a branched graph; run `makemigrations --merge`")

    if problems:
        print(f"Migration graph problems ({len(problems)}):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"All {scanned} migrations depend only on migrations the committed tree has.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
