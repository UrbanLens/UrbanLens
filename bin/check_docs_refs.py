#!/usr/bin/env python3
"""Fail if code cites a ``docs/`` path that does not exist, or a published file cites an internal one."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tomllib

#: ``docs/a/b.md``, ``../SiblingRepo/docs/a/b.md``, or any run of ``../``
#: walking up from the citing file. Extensions are listed rather than
#: open-ended so prose like "the docs/ directory" cannot match.
_CITATION = re.compile(r"(?:\.\./)*(?:[A-Za-z0-9_.-]+/)?docs/[A-Za-z0-9_./-]+\.(?:md|rst|json|txt|py)")

#: A markdown file named on its own - ``CHANGELOG.md``, ``TODO.md``.
#: Capitalised to avoid matching ``readme.md`` in prose.
#: Resolved against root plus ``docs/`` and ``docs/internal/``: bare ``FEATURES.md``
#: means ``docs/FEATURES.md``, bare ``INDEX.md`` means ``docs/internal/INDEX.md``.
_BARE_CITATION = re.compile(r"(?<![\w./-])([A-Z][A-Za-z0-9_-]*\.md)\b")

#: Files whose citations are checked. Everything else is prose about prose.
_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".yml", ".yaml", ".toml", ".sh", ".json", ".cfg", ".ini"}

#: This file's own docstring names the broken paths it was written for, which it
#: would otherwise report as live citations - the same trap
#: `bin/check_doc_line_refs.py` falls into, one level over.
_SKIP_FILES = {
    "bin/check_docs_refs.py",
    # Its fixtures are specimens, not citations: throwaway repositories built to
    # prove this script reports `docs/sub/GUIDE.md` when it should. Same trap as
    # this file's own docstring, one directory over.
    "src/urbanlens/dashboard/tests/hypothesis/test_docs_citation_resolution.py",
    # Declares the changelog it will generate ("changelog-path"), which is a
    # path this repository does not have yet rather than a citation of one.
    "release-please-config.json",
    # Freezes the notes earlier releases wrote into ApiRateLimit rows, which cite
    # documents since moved (docs/redata.md, docs/redata-cid-resolution.md). The
    # text has to match those rows exactly, so it cannot be repointed.
    "src/urbanlens/dashboard/migrations/0068_rate_limit_rows_from_any_release_take_0_9_0_defaults.py",
    # Names the index it audits, which is kept out of the public mirror; the
    # checker handles its absence, so the name is an input, not a pointer.
    "bin/check_docs_index.py",
    # `privatize` names paths the GitHub mirror still holds, which may no longer
    # exist here; each entry is an instruction to forge-sync, not a pointer.
    ".forgejo/sync.toml",
}

#: Build output that happens to be tracked. Minified bundles contain runs like
#: ``A5.md`` that are property accesses, not citations, and nothing in a
#: generated file is a pointer a reader would follow anyway.
_SKIP_PREFIXES = ("src/urbanlens/frontend/static/",)


#: Where a bare ``NAME.md`` may live besides the repository root.
_BARE_NAME_HOMES = ("docs", "docs/internal")

#: A path under a directory whose whole content is kept off the public mirror.
_INTERNAL_CITATION = re.compile(r"(?:\.\./)*(?:[A-Za-z0-9_.-]+/)?(?:docs|bin)/internal(?:/[A-Za-z0-9_./*-]*)?")

#: Always private, whatever `.forgejo/sync.toml` lists: forge-sync never mirrors its own configuration.
_ALWAYS_PRIVATE = (".forgejo/**",)

#: Published files that name an internal path as an input or output rather than as a pointer for a reader:
#: each reads or writes it, and works (or says why it cannot) when the mirror lacks it.
_MAY_NAME_INTERNAL = {
    # This file, which names the directories it polices.
    "bin/check_docs_refs.py",
    # Audits docs/internal/INDEX.md, and skips the audit where the file is absent.
    "bin/check_docs_index.py",
    # Writes its report to docs/internal/audits/ by default; -o chooses elsewhere.
    "bin/report_unused_methods.py",
    # Reads the open P# ids from docs/internal/PROBLEMS.md, and treats every id as retired without it.
    "bin/check_comment_hygiene.py",
    # Specimens for this checker, not citations.
    "src/urbanlens/dashboard/tests/hypothesis/test_docs_citation_resolution.py",
}


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """
    A forge-sync path glob as a regex over repo-relative paths.

    ``**/`` matches any number of leading directories (none included), ``**`` anything, ``*`` and ``?`` stay
    within one path segment. Patterns are anchored at the repository root, as `.forgejo/sync.toml` writes them.
    """
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def private_patterns(root: pathlib.Path) -> list[re.Pattern[str]]:
    """
    The `private` globs from `.forgejo/sync.toml`, plus the ones forge-sync always applies.

    Args:
        root: Repository root.

    Returns:
        One compiled pattern per glob; only `.forgejo/**` when the file is absent.
    """
    config = root / ".forgejo" / "sync.toml"
    globs = list(_ALWAYS_PRIVATE)
    if config.exists():
        globs += tomllib.loads(config.read_text(encoding="utf-8")).get("private", [])
    return [_glob_regex(glob) for glob in globs]


def internal_citations_from_public_files(root: pathlib.Path) -> dict[str, list[str]]:
    """
    Every published file that cites a path under ``docs/internal/`` or ``bin/internal/``.

    A file is published unless a `private` glob in `.forgejo/sync.toml` matches it. Such a citation dangles on the
    public mirror, and says out loud where the unpublished material is.

    Args:
        root: Repository root to scan.

    Returns:
        Citing file -> the internal paths it names.
    """
    private = private_patterns(root)
    found: dict[str, list[str]] = {}
    for name in _tracked_files(root):
        if name in _MAY_NAME_INTERNAL or name.startswith(_SKIP_PREFIXES) or any(pattern.match(name) for pattern in private):
            continue
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = sorted({hit.rstrip(".") for hit in _INTERNAL_CITATION.findall(text)})
        if hits:
            found[name] = hits
    return found


def _tracked_files(root: pathlib.Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True, cwd=root)
    return [name for name in out.stdout.split("\0") if name]


def _is_ignored(path: str, root: pathlib.Path) -> bool:
    # cwd=root, or this answers about the process's own directory rather than
    # the repository being checked.
    return subprocess.run(["git", "check-ignore", "-q", path], check=False, cwd=root).returncode == 0


def _resolves(citation: str, root: pathlib.Path, citing: pathlib.Path) -> bool:
    """
    Whether `citation` names something that exists.

    Tried against the repo root and against the citing file's own directory, because both spellings are in use:
    prose cites `docs/FEATURES.md` from the root, while code passes a path relative to itself
    (`join(import.meta.dir, "../../../../../../docs/...")`).

    A path into a sibling checkout counts as resolved when that checkout is absent: this repository cannot vouch
    for what it does not have. Such a path is judged from the repo root regardless of what cites it -
    ``../REData/docs/x.md`` means "beside this repository", not resolved relative to the citing file first.
    """
    if (root / citation).exists():
        return True
    if "/" not in citation and any((root / home / citation).exists() for home in _BARE_NAME_HOMES):
        return True
    resolved = ((root / citing).parent / citation).resolve()
    if resolved.exists():
        return True
    # Both spellings are tried for existence before this, so reaching here with
    # a sibling-shaped path means the sibling is the only reading left.
    for candidate in ((root / citation).resolve(), resolved):
        if _names_a_sibling(candidate, root):
            return _sibling_is_absent(candidate, root)
    return False


def _names_a_sibling(resolved: pathlib.Path, root: pathlib.Path) -> bool:
    """
    Whether `resolved` points into a directory beside this repository.

    Args:
        resolved: An absolute path.
        root: Repository root.

    Returns:
        True for a path inside the workspace but outside this repository.
        Anything further afield - the `../../../../../../docs/...` a test writes
        relative to its own file, which climbs past the workspace - is not a
        sibling citation and is judged as an ordinary one.
    """
    workspace = root.parent.resolve()
    return resolved.is_relative_to(workspace) and not resolved.is_relative_to(root.resolve())


def _sibling_is_absent(resolved: pathlib.Path, root: pathlib.Path) -> bool:
    """
    Whether the sibling checkout `resolved` names is missing from the workspace.

    Args:
        resolved: An absolute path into a sibling checkout.
        root: Repository root.

    Returns:
        True when the sibling directory does not exist, so the citation cannot be
        judged either way. False when it exists and the file does not - the check
        is skipped for want of evidence, not waived.
    """
    workspace = root.parent.resolve()
    return not (workspace / resolved.relative_to(workspace).parts[0]).is_dir()


def broken_citations(root: pathlib.Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """
    Every citation in `root` that does not resolve, split by what cites it.

    Args:
        root: Repository root to scan.

    Returns:
        ``(from code, from documents)``, each mapping a citation to the files that make it."""
    broken_code: dict[str, list[str]] = {}
    broken_docs: dict[str, list[str]] = {}
    checked: dict[tuple[str, str], bool] = {}

    for name in _tracked_files(root):
        if name in _SKIP_FILES or name.startswith(_SKIP_PREFIXES):
            continue
        path = root / name
        if path.suffix not in _CODE_SUFFIXES and path.suffix != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for citation in set(_CITATION.findall(text)) | set(_BARE_CITATION.findall(text)):
            # Keyed on citing *directory*: `_resolves` also tries the citing file's directory,
            # so the same string can resolve from one directory and not another; keying on
            # the string alone would let scan order decide for every other file.
            key = (citation, str(pathlib.PurePosixPath(name).parent))
            if key not in checked:
                checked[key] = _resolves(citation, root, pathlib.Path(name)) and not (not citation.startswith("../") and _is_ignored(citation, root))
            if checked[key]:
                continue
            bucket = broken_docs if path.suffix == ".md" else broken_code
            bucket.setdefault(citation, []).append(name)

    return broken_code, broken_docs


def main() -> int:
    """Report unresolvable docs citations, failing only on the ones in code."""
    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    broken_code, broken_docs = broken_citations(root)
    leaks = internal_citations_from_public_files(root)

    if broken_docs:
        print("Citations between documents that do not resolve (reported, not fatal):")
        for citation in sorted(broken_docs):
            print(f"  {citation}  <- {', '.join(sorted(broken_docs[citation]))}")
        print()

    if leaks:
        print("Published files cite internal paths (docs/internal/, bin/internal/), which the public mirror does not have:")
        for name in sorted(leaks):
            print(f"  {name}: {', '.join(leaks[name])}")
        print()
        print("Cite a published document instead, or make the citing file private in .forgejo/sync.toml.")
        print()

    if not broken_code:
        return 1 if leaks else 0

    print("Code and configuration cite docs/ paths that do not exist:")
    for citation in sorted(broken_code):
        print(f"  {citation}")
        for name in sorted(broken_code[citation]):
            print(f"      {name}")
    print()
    print("A moved document leaves its pointers behind. Find where it went with")
    print("  git log --diff-filter=D --name-only -- '*<basename>'")
    print("and repoint the citation, or name the sibling checkout explicitly")
    print("(../REData/docs/...) if it was never in this repository.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
