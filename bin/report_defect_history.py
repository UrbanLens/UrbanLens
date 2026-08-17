#!/usr/bin/env python3
"""Rank files by repair history, and surface fixes their own message calls partial.

Where bugs have been found is where bugs are. Two queries over git history
produced the most valuable findings of the 2026-08-17 audit, including both
money bugs in the billing ledger:

**Fix density** - the share of a file's commits that are fixes. A file whose
history is mostly repair is a file whose next change is likely to be repair.
``controllers/labels.py`` led this list at 8 of 18 commits and yielded three
real defects.

**The incomplete-fix query** - commits whose message says the fix reached one
place and implies others, in the author's own words: "like its sibling already
did", "the same fix", "also". Each names a spot where someone knew a pattern had
more than one instance. Following one of those ("the enrichment path catches
decompression bombs, like its sibling already did") ruled out a third instance;
following another led to the pay-what-you-want ledger's lost update.

Both are heuristics for *where to look*, not defect predictions. A file can be
fix-dense because it is old and well-maintained. The value is a ranked worklist
instead of a guess, on a codebase too large to read.

Usage:
    bin/report_defect_history.py [--since 2026-01-01] [--top N] [--min-commits N]
"""

from __future__ import annotations

import collections
import re
import subprocess
import sys

#: Message prefixes that mark a repair, matching this project's commit style.
_FIX_PATTERN = re.compile(r"^(fix|bugfix|hotfix)(\(|:|\s)", re.IGNORECASE)

#: Phrases an author uses when a fix knowingly covered one of several instances.
#: Deliberately broad - a false positive costs one commit read, and the query is
#: worthless if it only matches the one phrasing someone happened to remember.
_INCOMPLETE_PHRASES = (
    "like its sibling",
    "same fix",
    "the sibling",
    "also affected",
    "as well as",
    "incomplete",
    "partial fix",
    "first of",
    "one of several",
    "remaining",
    "still needs",
    "follow-up",
    "followup",
    "missed",
    "same shape",
    "same pattern",
    "elsewhere",
    "other call sites",
    "other paths",
)

#: Files with fewer commits than this have too little history for the ratio to
#: mean anything - one fix out of two commits is not a 50% defect rate.
_DEFAULT_MIN_COMMITS = 5


def _git(*args: str) -> str:
    """Run a git command and return its output.

    Args:
        *args: Arguments after ``git``.

    Returns:
        Standard output, stripped.
    """
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def fix_density(since: str, min_commits: int, top: int) -> None:
    """Print the files whose history is most heavily repair.

    Args:
        since: Git date to start from.
        min_commits: Ignore files with fewer commits than this.
        top: How many rows to print.
    """
    log = _git("log", f"--since={since}", "--pretty=format:%H%x00%s", "--name-only")
    commits: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for line in log.splitlines():
        if "\x00" in line:
            sha, subject = line.split("\x00", 1)
            current = sha
            commits[sha] = (subject, [])
        elif line.strip() and current:
            commits[current][1].append(line.strip())

    totals: collections.Counter[str] = collections.Counter()
    fixes: collections.Counter[str] = collections.Counter()
    for subject, files in commits.values():
        is_fix = bool(_FIX_PATTERN.match(subject))
        for path in files:
            if not path.endswith((".py", ".ts", ".tsx", ".html", ".scss")):
                continue
            totals[path] += 1
            if is_fix:
                fixes[path] += 1

    ranked = [(path, fixes[path], total, fixes[path] / total) for path, total in totals.items() if total >= min_commits and fixes[path]]
    ranked.sort(key=lambda row: (row[3], row[1]), reverse=True)

    print(f"=== Fix density (files with >= {min_commits} commits since {since}) ===")
    print(f"{'file':64} {'fixes':>6} {'commits':>8} {'share':>7}")
    print("-" * 88)
    for path, fix_count, total, share in ranked[:top]:
        print(f"{path[-64:]:64} {fix_count:>6} {total:>8} {share:>6.0%}")
    if not ranked:
        print("(no file has both enough history and a repair in it)")
    print()


def incomplete_fixes(since: str, top: int) -> None:
    """Print fixes whose own message suggests they covered one of several sites.

    Args:
        since: Git date to start from.
        top: How many commits to print.
    """
    log = _git("log", f"--since={since}", "--pretty=format:%h%x00%s%x00%b%x00%x00")
    hits = []
    for entry in log.split("\x00\x00"):
        parts = entry.strip().split("\x00")
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1]
        body = parts[2] if len(parts) > 2 else ""
        haystack = f"{subject}\n{body}".lower()
        matched = [phrase for phrase in _INCOMPLETE_PHRASES if phrase in haystack]
        if matched and _FIX_PATTERN.match(subject):
            hits.append((sha, subject, matched))

    print("=== Fixes whose message implies more instances exist ===")
    print("Read each and ask: did it reach every instance, or only the one in front of it?")
    print()
    for sha, subject, matched in hits[:top]:
        print(f"  {sha}  {subject[:88]}")
        print(f"           phrase(s): {', '.join(matched[:4])}")
    if not hits:
        print("  (none - either the fixes were complete or they did not say so)")
    print()


def main(argv: list[str]) -> int:
    """Print both reports.

    Args:
        argv: Command-line arguments.

    Returns:
        Always 0 - this reports, it does not gate.
    """
    since = argv[argv.index("--since") + 1] if "--since" in argv else "2 years ago"
    top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 20
    min_commits = int(argv[argv.index("--min-commits") + 1]) if "--min-commits" in argv else _DEFAULT_MIN_COMMITS

    fix_density(since, min_commits, top)
    incomplete_fixes(since, top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
