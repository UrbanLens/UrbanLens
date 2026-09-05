#!/usr/bin/env python3
"""Fail if a tracked TypeScript file belongs to no ``tsconfig.json``.

``bun run typecheck`` checks the *projects*, not the repository. A file outside
every project's ``include`` is not reported as unchecked - it is simply never
read, and the command still exits 0. The pre-commit ``tsc`` hook makes that
worse: it fires on every ``.ts``/``.tsx`` path, so editing an uncovered file
runs a typecheck that does not look at the file that triggered it, and passing
means nothing about the change.

That was the state on 2026-09-04: 87 tracked ``.ts`` files sat outside the root
project, 84 of them covered by ``tests/integration/tsconfig.json`` that nothing
ran, and three covered by nothing at all.

Coverage here means *listed in a project*, which is weaker than *checked*: a
project nobody runs still covers its files. `package.json`'s ``typecheck``
script is what closes that half, and it names every config this walks.

Any deliberate exception goes in `_UNCOVERED` with the reason it is there, so
the gap is a line someone chose rather than an absence nobody can see.

Exits non-zero listing each uncovered file. Safe to run by hand from the repo
root.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

#: Files deliberately in no project, and why. Each is still a gap - this is a
#: record of a decision, not an approval.
_UNCOVERED = {
    "src/urbanlens/dashboard/frontend/browser/floorplan-editor.test.ts": (
        "bun-types is pinned at 1.1.6 against Bun 1.3.14, and its `expect` predates the second message argument these tests pass - 81 spurious TS2554s. Adding them to the root project needs the dependency bumped first."
    ),
    "src/urbanlens/dashboard/frontend/browser/harness-parity.test.ts": ("Same bun-types pin as its sibling above; kept together so both land in one pass."),
}

#: What TypeScript excludes when a config says nothing. `outDir` is not among
#: these because no config here sets one; a config that does needs it added.
_DEFAULT_EXCLUDES = ("node_modules", "bower_components", "jspm_packages")

#: What an `include` entry naming a bare directory expands to, per TypeScript's
#: own rule: the supported extensions, recursively.
_DIRECTORY_SUFFIXES = (".ts", ".tsx", ".d.ts")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate one tsconfig include/exclude glob into a whole-path regex.

    Handles the three wildcards TypeScript documents: ``**`` for any number of
    path segments, ``*`` for any run of characters within one segment, and ``?``
    for one such character.

    Args:
        pattern: A glob, relative to the config that declared it.

    Returns:
        A compiled pattern matching a whole relative path.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile(f"^{''.join(out)}$")


def covered_paths(config_path: str, config: dict, candidates: list[str]) -> set[str]:
    """Which of `candidates` the project at `config_path` lists.

    Args:
        config_path: Repo-relative path of the ``tsconfig.json``.
        config: Its parsed contents.
        candidates: Repo-relative paths to test.

    Returns:
        The subset the project includes and does not exclude, plus anything the
        config names outright in ``files``.
    """
    directory = pathlib.PurePosixPath(config_path).parent
    prefix = "" if str(directory) == "." else f"{directory}/"

    named = {f"{prefix}{entry}" for entry in config.get("files") or []}
    # `include` defaults to everything *only* when the config lists no `files`.
    # With `files` present TypeScript defaults it to nothing, and reading it as
    # `**/*` would report a whole directory as covered by a project that
    # compiles one file of it.
    default_include = [] if "files" in config else ["**/*"]
    includes = config.get("include") or default_include
    excludes = list(config.get("exclude") or []) + list(_DEFAULT_EXCLUDES)

    def expand(entry: str) -> list[re.Pattern[str]]:
        # A bare directory means everything under it; TypeScript infers the
        # extensions rather than requiring the glob.
        if not entry.endswith(_DIRECTORY_SUFFIXES) and "*" not in entry and "?" not in entry:
            return [glob_to_regex(f"{prefix}{entry.rstrip('/')}/**/*{suffix}") for suffix in _DIRECTORY_SUFFIXES]
        return [glob_to_regex(f"{prefix}{entry}")]

    included = [pattern for entry in includes for pattern in expand(entry)]
    # An exclude entry names a subtree as often as a file, so it matches the
    # path itself or anything under it.
    excluded = [pattern for entry in excludes for pattern in (glob_to_regex(f"{prefix}{entry}"), glob_to_regex(f"{prefix}{entry.rstrip('/')}/**"))]

    matched = {path for path in candidates if any(pattern.match(path) for pattern in included) and not any(pattern.match(path) for pattern in excluded)}
    # `files` is not filtered by `exclude`: TypeScript compiles what it names.
    return matched | (named & set(candidates))


def _projects_not_typechecked(root: pathlib.Path, configs: list[str]) -> list[str]:
    """Which tracked projects `package.json`'s `typecheck` script never reads.

    Coverage by a project only means anything if something runs that project.
    `tests/integration/tsconfig.json` existed - and covered 84 files - while no
    command in the repository invoked it, which is the state this check was
    written for; a third config added tomorrow would recreate it silently.

    Args:
        root: Repository root.
        configs: Repo-relative paths of every tracked ``tsconfig.json``.

    Returns:
        The configs the script does not name, sorted.
    """
    try:
        script = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {}).get("typecheck", "")
    except (OSError, json.JSONDecodeError):
        return sorted(configs)
    return sorted(path for path in configs if path not in script)


def main() -> int:
    """Report every tracked TypeScript file no project lists."""
    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    tracked = [name for name in subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True, cwd=root).stdout.split("\0") if name]

    sources = [name for name in tracked if name.endswith((".ts", ".tsx")) and not name.endswith(".d.ts")]
    configs = [name for name in tracked if pathlib.PurePosixPath(name).name == "tsconfig.json"]

    covered: set[str] = set()
    for config_path in configs:
        try:
            config = json.loads((root / config_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"{config_path} is not parseable as JSON: {exc}")
            return 1
        if "extends" in config:
            # Resolving it means merging the base's include/exclude rebased on
            # the base's own directory. Nothing here needs that yet, and a
            # half-implementation would silently widen coverage - the failure
            # this whole check exists to prevent.
            print(f"{config_path} uses `extends`, which this check cannot resolve. Inline its include/exclude, or teach {pathlib.Path(__file__).name} to merge them.")
            return 1
        covered |= covered_paths(config_path, config, sources)

    unrun = _projects_not_typechecked(root, configs)
    uncovered = sorted(set(sources) - covered)
    unexpected = [path for path in uncovered if path not in _UNCOVERED]
    stale = sorted(path for path in _UNCOVERED if path in covered or path not in sources)

    if not unexpected and not stale and not unrun:
        for path in uncovered:
            print(f"note: {path} is in no tsconfig - {_UNCOVERED[path]}")
        return 0

    if unrun:
        print(f"tsconfig projects that `bun run typecheck` does not run ({len(unrun)}):")
        for path in unrun:
            print(f"  {path}")
        print()
        print("A project nothing runs covers its files on paper only, and the pre-commit tsc")
        print("hook delegates to this same script. Add `&& tsc --noEmit -p <path>` to the")
        print("`typecheck` script in package.json.")
    if unexpected:
        print(f"TypeScript files in no tsconfig.json ({len(unexpected)}):")
        for path in unexpected:
            print(f"  {path}")
        print()
        print("`bun run typecheck` checks projects, not files, so these are never read - and")
        print("the pre-commit tsc hook still fires when you edit one. Add them to a project's")
        print("`include`, or list them in _UNCOVERED here with the reason they cannot be.")
    if stale:
        print(f"_UNCOVERED entries that are no longer uncovered or no longer exist ({len(stale)}):")
        for path in stale:
            print(f"  {path}")
        print()
        print("Delete these: an exception nobody removes reads as a rule.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
