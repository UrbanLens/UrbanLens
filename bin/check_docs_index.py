#!/usr/bin/env python3
"""Fail if `docs/INDEX.md` and the entries it indexes have drifted apart.

The index is the allocator: `docs/README.md` says a duplicate id should become a
merge conflict rather than a silent collision, and `CLAUDE.md` sends every
session to `INDEX.md` before it reads anything else in `docs/`. Neither promise
survives without something checking it - the first generated index in this repo
gave `P1` to both a problem entry and a design document, and nothing noticed.

Seven invariants, all cheap and all unambiguous:

1. No id appears twice - in the index, in `PROBLEMS.md`, or in the archive.
2. Every `## P# --` heading in `PROBLEMS.md` has a row, and vice versa.
3. A `P#` row's claim matches its heading, so grepping the index finds the same
   sentence the entry opens with.
4. Every status is one the record's own prefix allows (`docs/README.md`).
5. The "Next free id" header is actually free - one past the highest id ever
   allocated, counting ids that have since been archived.
6. An archived id is not still live in `PROBLEMS.md` or `INDEX.md`; resolving an
   entry moves it, and a half-move leaves two copies to disagree.
7. No id appears twice inside the archive.

Invariants 5-7 need `archive/PROBLEMS-ARCHIVE.md` to carry the id of what it
holds. Resolved entries are removed from the index, so without that the highest
allocated id *falls* as work is finished, and this check would then demand the
next writer reuse the id of the entry just archived - the collision the index
exists to prevent. An archived entry keeps its `id:` metadata line for that
reason; it is also what lets a citation of `P70` still be resolvable after the
entry it names has been fixed.

Deliberately NOT checked: whether a claim is true, and whether a `status` is
current. Both need a human reading the code, and a checker that guesses at them
would be wrong in a way nobody could act on.

Exits non-zero listing each drift. Safe to run by hand from the repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: Status values each prefix allows, from the table in `docs/README.md`.
_STATUSES = {
    "P": {"open", "blocking", "fixed"},
    "I": {"unvalidated", "actionable", "absorbed"},
    "D": {"accepted", "superseded"},
    "X": {"holds", "collapsed", "untestable", "disqualified"},
    "T": {"open", "blocked", "done"},
    "PL": {"live", "superseded"},
    "R": {"current", "stale"},
    "N": {"current", "stale"},
}


def _sort_key(ident: str) -> tuple[str, int]:
    """Split `PL7` into its prefix and number, so ids sort numerically."""
    match = re.fullmatch(r"([A-Z]+)(\d+)", ident)
    return (match.group(1), int(match.group(2))) if match else (ident, 0)


_ROW = re.compile(r"^\|\s*([A-Z]+)(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)
_HEADING = re.compile(r"^## (P\d+) — (.+)$", re.MULTILINE)
_NEXT_FREE = re.compile(r"^\*\*Next free id:\*\*\s*(.+)$", re.MULTILINE)

#: The metadata line an entry opens with, wherever it lives: ``id: P70`` in
#: backticks at the start of a line. The archive keeps it so an id stays
#: allocated after the entry stops being live.
_ARCHIVED_ID = re.compile(r"^`id: ([A-Z]+)(\d+)`", re.MULTILINE)


def audit(index: str, problems: str, archive: str) -> list[str]:
    """Report every way the index, the live entries and the archive disagree.

    Args:
        index: Contents of `docs/INDEX.md`.
        problems: Contents of `docs/PROBLEMS.md`.
        archive: Contents of `docs/archive/PROBLEMS-ARCHIVE.md`.

    Returns:
        One human-readable line per drift, empty when everything agrees.
    """
    rows = _ROW.findall(index)
    headings = _HEADING.findall(problems)
    archived = _ARCHIVED_ID.findall(archive)

    failures: list[str] = []

    seen: dict[str, int] = {}
    highest: dict[str, int] = {}
    for prefix, number, status, _updated, claim, _path in rows:
        ident = f"{prefix}{number}"
        seen[ident] = seen.get(ident, 0) + 1
        highest[prefix] = max(highest.get(prefix, 0), int(number))
        allowed = _STATUSES.get(prefix)
        if allowed and status not in allowed:
            failures.append(f"  {ident}: status {status!r} is not one of {sorted(allowed)}")
        if claim.endswith("."):
            failures.append(f"  {ident}: claim ends in a full stop; the index is one line per record, not prose")

    failures.extend(f"  {ident} appears {count} times" for ident, count in sorted(seen.items(), key=lambda kv: _sort_key(kv[0])) if count > 1)

    archived_seen: dict[str, int] = {}
    for prefix, number in archived:
        ident = f"{prefix}{number}"
        archived_seen[ident] = archived_seen.get(ident, 0) + 1
        highest[prefix] = max(highest.get(prefix, 0), int(number))

    failures.extend(f"  {ident} appears {count} times in the archive" for ident, count in sorted(archived_seen.items(), key=lambda kv: _sort_key(kv[0])) if count > 1)

    indexed = {f"{p}{n}": claim for p, n, _s, _u, claim, _path in rows}
    # Counted before collapsing: `dict(headings)` folds a duplicated entry into
    # one and compares only the last copy's claim against the index row, so a
    # copy-paste sharing an id passes clean.
    heading_counts: dict[str, int] = {}
    for ident, _claim in headings:
        heading_counts[ident] = heading_counts.get(ident, 0) + 1
    failures.extend(f"  {ident} has {count} headings in PROBLEMS.md" for ident, count in sorted(heading_counts.items(), key=lambda kv: _sort_key(kv[0])) if count > 1)
    entries = dict(headings)
    problem_rows = {ident for ident in indexed if re.fullmatch(r"P\d+", ident)}
    for ident in sorted(set(entries) - problem_rows, key=_sort_key):
        failures.append(f"  {ident} has a heading in PROBLEMS.md but no row in INDEX.md")
    for ident in sorted(problem_rows - set(entries), key=_sort_key):
        failures.append(f"  {ident} has a row in INDEX.md but no heading in PROBLEMS.md")
    for ident in sorted(set(entries) & problem_rows, key=_sort_key):
        if entries[ident].strip() != indexed[ident].strip():
            failures.append(f"  {ident}: heading and index row disagree\n      heading: {entries[ident]}\n      index:   {indexed[ident]}")

    for ident in sorted(set(archived_seen) & (set(entries) | set(indexed)), key=_sort_key):
        where = " and ".join(name for name, live in (("PROBLEMS.md", ident in entries), ("INDEX.md", ident in indexed)) if live)
        failures.append(f"  {ident} is archived but still live in {where}; archiving moves an entry, it does not copy it")

    declared = _NEXT_FREE.search(index)
    if not declared:
        failures.append("  the 'Next free id' header is missing; it is how the next writer allocates")
    else:
        for prefix, number in re.findall(r"`([A-Z]+)(\d+)`", declared.group(1)):
            expected = highest.get(prefix, 0) + 1
            if int(number) != expected:
                failures.append(f"  next free {prefix} is {number}, but {prefix}{highest.get(prefix, 0)} is allocated - should be {prefix}{expected}")

    return failures


def main() -> int:
    """Read the three files and print whatever `audit` finds wrong."""
    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    index_path = root / "docs/INDEX.md"
    problems_path = root / "docs/PROBLEMS.md"
    archive_path = root / "docs/archive/PROBLEMS-ARCHIVE.md"
    if not index_path.is_file():
        print("docs/INDEX.md is missing. CLAUDE.md and .claude/agents/ both send readers to it.")
        return 1

    failures = audit(
        index_path.read_text(encoding="utf-8"),
        problems_path.read_text(encoding="utf-8") if problems_path.is_file() else "",
        archive_path.read_text(encoding="utf-8") if archive_path.is_file() else "",
    )

    if not failures:
        return 0
    print(f"docs/INDEX.md has drifted from the entries it indexes ({len(failures)}):")
    print("\n".join(failures))
    print()
    print("The index is the allocator - see docs/README.md. Add the row in the same")
    print("commit as the entry, and keep the row's claim identical to the heading so")
    print("one grep of the index answers the question.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
