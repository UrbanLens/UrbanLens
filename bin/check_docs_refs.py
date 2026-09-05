#!/usr/bin/env python3
"""Fail if code cites a ``docs/`` path that does not exist.

Documents get moved into subdirectories and the pointers to them do not follow.
A 2026-09-04 sweep found 39 such citations across 33 files naming 8 documents -
``docs/GOALS_CODE_AUDIT.md`` (16 citations) and ``docs/TEST_COVERAGE_GAPS.md``
(7) had both moved under ``docs/audits/`` a week earlier, and
``docs/PROBLEMS-ARCHIVE.md`` pointed at a file that had been deleted from git
without its replacement being added, so a fresh checkout had neither path.

Only citations from **code and configuration** fail the check. A citation from
one document to another is reported and does not fail, because several
historical reports deliberately name planning documents that were never written
- that absence is itself recorded, in ``docs/PROBLEMS.md``. Making those fail
would leave this permanently red, which is the same as switching it off.

Paths into a sibling checkout (``../REData/docs/...``) are verified when that
checkout is present and skipped when it is not, so this still works in CI.
A gitignored target fails like a missing one. It resolves for whoever wrote
the citation and for nobody else, which is the same outcome as a broken path
for every other reader - and it is how seven citations of
``docs/notes/ai/completed.md``, a file never committed, stayed green.

Exits non-zero listing each unresolvable citation. Safe to run by hand from the
repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: ``docs/a/b.md``, ``../SiblingRepo/docs/a/b.md``, or any run of ``../``
#: walking up from the citing file. Extensions are listed rather than
#: open-ended so prose like "the docs/ directory" cannot match.
_CITATION = re.compile(r"(?:\.\./)*(?:[A-Za-z0-9_.-]+/)?docs/[A-Za-z0-9_./-]+\.(?:md|rst|json|txt|py)")

#: A markdown file named on its own - ``ROADMAP.md``, ``TODO.md``. The
#: ``docs/``-prefixed form above cannot see these, which is how eleven citations
#: of a root ``TODO.md`` survived its rename to ``ROADMAP.md`` in ``3f12e875``.
#: Capitalised because that is the convention for the repository-level documents
#: this is about, and lowercase would match every ``readme.md`` in prose.
#: Resolved against ``docs/`` as well as the root, since a bare ``PROBLEMS.md``
#: means ``docs/PROBLEMS.md`` in 22 files here and is not a defect.
_BARE_CITATION = re.compile(r"(?<![\w./-])([A-Z][A-Za-z0-9_-]*\.md)\b")

#: Files whose citations are checked. Everything else is prose about prose.
_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".yml", ".yaml", ".toml", ".sh", ".json", ".cfg", ".ini"}

#: This file's own docstring names the broken paths it was written for, which it
#: would otherwise report as live citations - the same trap
#: `bin/check_doc_line_refs.py` falls into, one level over.
_SKIP_FILES = {
    "bin/check_docs_refs.py",
    # Declares the changelog it will generate ("changelog-path"), which is a
    # path this repository does not have yet rather than a citation of one.
    "release-please-config.json",
}

#: Build output that happens to be tracked. Minified bundles contain runs like
#: ``A5.md`` that are property accesses, not citations, and nothing in a
#: generated file is a pointer a reader would follow anyway.
_SKIP_PREFIXES = ("src/urbanlens/frontend/static/",)


def _tracked_files(root: pathlib.Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True, cwd=root)
    return [name for name in out.stdout.split("\0") if name]


def _is_ignored(path: str, root: pathlib.Path) -> bool:
    # cwd=root, or this answers about the process's own directory rather than
    # the repository being checked.
    return subprocess.run(["git", "check-ignore", "-q", path], check=False, cwd=root).returncode == 0


def _resolves(citation: str, root: pathlib.Path, citing: pathlib.Path) -> bool:
    """Whether `citation` names something that exists.

    Tried against the repo root and against the citing file's own directory,
    because both spellings are in use: prose cites `docs/NOTES.md` from the
    root, while code passes a path relative to itself
    (`join(import.meta.dir, "../../../../../../docs/...")`).

    A path into a sibling checkout counts as resolved when that checkout is
    absent: this repository cannot vouch for what it does not have, and failing
    on it would make the check depend on how a developer laid out their
    workspace.
    """
    if (root / citation).exists():
        return True
    if "/" not in citation and (root / "docs" / citation).exists():
        return True
    resolved = ((root / citing).parent / citation).resolve()
    if resolved.exists():
        return True
    if resolved.is_relative_to(root.resolve()):
        return False
    # Outside this repository: a sibling checkout, which can only be verified
    # when it happens to be present next to this one.
    relative = resolved.relative_to(root.parent.resolve())
    return not (root.parent / relative.parts[0]).is_dir()


def broken_citations(root: pathlib.Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Every citation in `root` that does not resolve, split by what cites it.

    Args:
        root: Repository root to scan.

    Returns:
        ``(from code, from documents)``, each mapping a citation to the files
        that make it. Only the first is fatal - see the module docstring.
    """
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
            # Keyed on the citing *directory*, not just the citation: `_resolves`
            # also tries the citing file's own directory, so the same string can
            # legitimately resolve from one directory and not another. Keying on
            # the string alone let whichever file was scanned first decide for
            # every other - in both directions.
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

    if broken_docs:
        print("Citations between documents that do not resolve (reported, not fatal):")
        for citation in sorted(broken_docs):
            print(f"  {citation}  <- {', '.join(sorted(broken_docs[citation]))}")
        print()

    if not broken_code:
        return 0

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
