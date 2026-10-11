#!/usr/bin/env python3
"""Fail if `docs/INDEX.md` (where present) has drifted from its own rules, or `docs/adr/` reuses a number."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: Status values each prefix allows.
_STATUSES = {
    "I": {"unvalidated", "actionable", "absorbed"},
    "X": {"holds", "collapsed", "untestable", "disqualified"},
    "PL": {"live", "superseded"},
    "R": {"current", "stale"},
    "N": {"current", "stale"},
}

#: Prefixes whose records moved out of the index, and where they went.
_RETIRED = {
    "P": "problems are GitHub issues",
    "T": "tasks are GitHub issues",
    "D": "decisions are ADRs in docs/adr/",
}


def _sort_key(ident: str) -> tuple[str, int]:
    """Split `PL7` into its prefix and number, so ids sort numerically."""
    match = re.fullmatch(r"([A-Z]+)(\d+)", ident)
    return (match.group(1), int(match.group(2))) if match else (ident, 0)


_ROW = re.compile(r"^\|\s*([A-Z]+)(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)
_NEXT_FREE = re.compile(r"^\*\*Next free id:\*\*\s*(.+)$", re.MULTILINE)
_ADR_NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")


def audit(index: str, adr_names: list[str]) -> list[str]:
    """Report every way the index or the ADR directory breaks its rules.

    Args:
        index: Contents of `docs/INDEX.md`.
        adr_names: File names in `docs/adr/`.

    Returns:
        One human-readable line per drift, empty when everything agrees."""
    return audit_index(index) + audit_adrs(adr_names)


def audit_index(index: str) -> list[str]:
    """Report every way the index breaks its rules.

    Args:
        index: Contents of `docs/INDEX.md`.

    Returns:
        One human-readable line per drift, empty when everything agrees."""
    failures: list[str] = []

    seen: dict[str, int] = {}
    highest: dict[str, int] = {}
    for prefix, number, status, _updated, claim, _path in _ROW.findall(index):
        ident = f"{prefix}{number}"
        if prefix in _RETIRED:
            failures.append(f"  {ident}: {_RETIRED[prefix]} now, not index rows")
            continue
        seen[ident] = seen.get(ident, 0) + 1
        highest[prefix] = max(highest.get(prefix, 0), int(number))
        allowed = _STATUSES.get(prefix)
        if allowed is None:
            failures.append(f"  {ident}: unknown prefix {prefix!r}")
        elif status not in allowed:
            failures.append(f"  {ident}: status {status!r} is not one of {sorted(allowed)}")
        if claim.endswith("."):
            failures.append(f"  {ident}: claim ends in a full stop; the index is one line per record, not prose")

    failures.extend(f"  {ident} appears {count} times" for ident, count in sorted(seen.items(), key=lambda kv: _sort_key(kv[0])) if count > 1)

    declared = _NEXT_FREE.search(index)
    if not declared:
        failures.append("  the 'Next free id' header is missing; it is how the next writer allocates")
    else:
        for prefix, number in re.findall(r"`([A-Z]+)(\d+)`", declared.group(1)):
            if prefix in _RETIRED:
                failures.append(f"  next free names {prefix}{number}, but {_RETIRED[prefix]} now")
                continue
            expected = highest.get(prefix, 0) + 1
            if int(number) != expected:
                failures.append(f"  next free {prefix} is {number}, but {prefix}{highest.get(prefix, 0)} is allocated - should be {prefix}{expected}")

    return failures


def audit_adrs(adr_names: list[str]) -> list[str]:
    """Report every ADR file that is misnamed or reuses another's number.

    Args:
        adr_names: File names in `docs/adr/`.

    Returns:
        One human-readable line per drift, empty when everything agrees."""
    failures: list[str] = []
    numbers: dict[str, list[str]] = {}
    for name in adr_names:
        if name == "README.md":
            continue
        match = _ADR_NAME.match(name)
        if not match:
            failures.append(f"  docs/adr/{name}: not named like 0001-kebab-slug.md")
            continue
        numbers.setdefault(match.group(1), []).append(name)
    failures.extend(f"  ADR-{number} is used by {', '.join(sorted(names))}" for number, names in sorted(numbers.items()) if len(names) > 1)

    return failures


def main() -> int:
    """Read the index and the ADR directory, and print whatever `audit` finds wrong."""
    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    index_path = root / "docs/INDEX.md"
    adr_dir = root / "docs/adr"
    adr_names = sorted(path.name for path in adr_dir.glob("*.md")) if adr_dir.is_dir() else []
    # The index is kept out of the public mirror, so a checkout of that mirror has
    # only the ADR directory to audit.
    failures = audit(index_path.read_text(encoding="utf-8"), adr_names) if index_path.is_file() else audit_adrs(adr_names)

    if not failures:
        return 0
    print(f"docs/INDEX.md or docs/adr/ has drifted ({len(failures)}):")
    print("\n".join(failures))
    print()
    print("The index is the allocator for the prefixes it still holds.")
    print("Problems and tasks are GitHub issues; decisions are ADRs in docs/adr/.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
