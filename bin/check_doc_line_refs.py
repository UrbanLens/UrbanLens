#!/usr/bin/env python3
"""Fail if a documentation citation points past the end of the file it names.

``docs/PROBLEMS.md`` and the audit report cite code as ``path/to/file.py:1234``,
and those numbers drift silently as the code moves - the file keeps existing and
the line keeps existing, so nothing complains. A 2026-08-17 audit of the 161
resolvable citations found 23 pointing somewhere other than what they described,
one of them past the end of its file entirely: ``controllers/friendship.py:649``
in a 583-line file. A reader following that citation lands on unrelated code, or
on nothing, and the note it was supposed to support reads as wrong.

This checks only the half of that which is unambiguous: **the cited line must
exist**. That is a fact about the file, needs no guess about intent, and is
currently true everywhere - so it can be enforced rather than merely reported.

The other half - a line that exists but no longer holds what the prose claims -
is deliberately *not* enforced here. Detecting it means matching identifiers
named in the surrounding prose, which produces judgement calls a CI job should
not be making: several such citations name symbols that have since been renamed
or deleted, where the right repair is rewriting the sentence, not the number.
``--report-drift`` prints those as information for a human.

The prose it reads for those identifiers is the whole wrapped block the citation
sits in, not the one line - markdown wraps well inside a sentence, so the symbol
a citation is about lands on a neighbouring line about as often as beside it.
The block stops at the next list item rather than the next blank line, or on a
bullet list every sibling bullet's identifiers would vouch for this one.

Exits non-zero listing each citation past end-of-file. Safe to run by hand from
the repo root.
"""

from __future__ import annotations

import collections
import pathlib
import re
import subprocess
import sys

#: ``path.py:123`` or ``path.py:123-140``. Extensions are listed rather than
#: open-ended so prose like "Django 5.1:" or a version string can't match.
_CITATION = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|ts|tsx|html|scss|js|yml|yaml|toml)):(\d+)(?!\d)")

#: Backticked identifiers in the same sentence, used only by --report-drift.
#:
#: A trailing ``()`` and a dotted suffix are both allowed and both dropped: the
#: prose writes `plan_merge_conflicts()` and `MediaKind.VIDEO`, while the code
#: defines ``def plan_merge_conflicts(`` and ``VIDEO = "video"`` under a
#: ``class MediaKind``. Anchoring on the leading segment is what matches both.
_IDENTIFIER = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?`")

#: Short names match too much prose ("`slug`", "`name`") to be a useful anchor.
_MIN_IDENTIFIER_LENGTH = 5

#: Starts a new list item, so the wrapped block a citation belongs to ends here.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _without_struck_text(lines: list[str]) -> list[str]:
    """The document with every ``~~struck~~`` span blanked out.

    A struck citation is a record of where a defect *was*, kept beside the note
    that it is fixed - the same thing the archive is, one sentence wide.
    Renumbering it would point the sentence at the repaired code and make the
    description read as false, so drift inside one is not drift. Spans run
    across lines, since the prose wraps.

    Args:
        lines: Every line of the document.

    Returns:
        The same lines, with struck spans replaced by spaces so that every
        surviving citation keeps its line and column.
    """
    result: list[str] = []
    struck = False
    for line in lines:
        # A span never crosses a paragraph break. Without this an unbalanced
        # ``~~`` would blank the whole rest of the document, and the enforced
        # past-end-of-file check would stop covering it without saying so.
        if not line.strip():
            struck = False
        out: list[str] = []
        position = 0
        while position < len(line):
            if line.startswith("~~", position):
                struck = not struck
                out.append("  ")
                position += 2
                continue
            out.append(" " if struck else line[position])
            position += 1
        result.append("".join(out))
    return result


def _citation_block(lines: list[str], index: int) -> range:
    """The wrapped prose block the line at `index` belongs to.

    Markdown wraps at well under the length of a sentence naming a symbol, so
    the identifier a citation is about is very often on the line above or below
    it rather than beside it. Reading only the citing line is what made the
    drift report name `hidden` as the anchor for a `meta.py:37` citation whose
    actual subject, `ICON_CATEGORIES`, sat one line up.

    A block runs to a blank line, a heading, or the start of the next list item
    - not to the next blank line alone, which on a bullet list would swallow
    every sibling bullet and let any of their identifiers vouch for this one.

    Args:
        lines: Every line of the document.
        index: Zero-based index of the citing line.

    Returns:
        The range of line indices forming that block.
    """

    def breaks(line: str) -> bool:
        return not line.strip() or line.lstrip().startswith("#")

    start = index
    while start > 0 and not breaks(lines[start - 1]) and not _LIST_ITEM.match(lines[start]):
        start -= 1

    end = index + 1
    while end < len(lines) and not breaks(lines[end]) and not _LIST_ITEM.match(lines[end]):
        end += 1

    return range(start, end)


def _tracked_files_by_suffix() -> dict[str, list[str]]:
    """Index every tracked file under each of its path suffixes.

    Citations are written relative to wherever the author was reading -
    ``controllers/maps.py``, ``dashboard/controllers/maps.py`` and the full path
    all appear - so resolution is by suffix, and only an unambiguous match counts.

    Returns:
        Mapping of path suffix to the tracked paths ending with it.
    """
    listing = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    index: dict[str, list[str]] = collections.defaultdict(list)
    for path in listing:
        parts = path.split("/")
        for start in range(len(parts)):
            index["/".join(parts[start:])].append(path)
    return index


def _documentation_files() -> list[pathlib.Path]:
    """Every markdown file whose citations are checked."""
    return sorted(pathlib.Path("docs").rglob("*.md"))


def check(*, report_drift: bool = False) -> int:
    """Report citations pointing past end-of-file, and optionally suspected drift.

    Args:
        report_drift: Also print citations whose line exists but does not appear
            to hold the identifier named beside it. Informational only.

    Returns:
        Process exit code: non-zero when any citation points past end-of-file.
    """
    index = _tracked_files_by_suffix()
    past_end: list[str] = []
    suspected_drift: list[str] = []

    for document in _documentation_files():
        lines = document.read_text(encoding="utf-8").splitlines()
        # Struck text is exempt from the drift report but not from the check
        # above it: a citation nobody can follow is broken whether or not the
        # sentence around it says the defect is gone.
        unstruck = _without_struck_text(lines)
        for line_number, line in enumerate(lines, 1):
            block = "\n".join(unstruck[i] for i in _citation_block(lines, line_number - 1))
            identifiers = [name for name in _IDENTIFIER.findall(block) if len(name) >= _MIN_IDENTIFIER_LENGTH]
            for citation in _CITATION.finditer(line):
                cited_path, cited_line = citation.group(1).lstrip("./"), int(citation.group(2))
                matches = index.get(cited_path, [])
                if len(matches) != 1:
                    continue
                source = pathlib.Path(matches[0]).read_text(encoding="utf-8").splitlines()
                if cited_line > len(source):
                    past_end.append(f"{document}:{line_number}: cites {cited_path}:{cited_line}, but that file has {len(source)} lines")
                    continue
                struck = unstruck[line_number - 1][citation.start() : citation.end()] != citation.group(0)
                if report_drift and identifiers and not struck:
                    window = "\n".join(source[max(0, cited_line - 6) : cited_line + 5])
                    if not any(name in window for name in identifiers):
                        suspected_drift.append(f"{document}:{line_number}: {cited_path}:{cited_line} does not mention {identifiers[:3]}")

    if suspected_drift:
        print(f"Suspected drift ({len(suspected_drift)}) - informational, needs a human:")
        for entry in suspected_drift:
            print(f"  {entry}")
        print()

    if past_end:
        print(f"Documentation citations past end-of-file ({len(past_end)}):")
        for entry in past_end:
            print(f"  {entry}")
        return 1

    print("All documentation citations point at a line that exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check(report_drift="--report-drift" in sys.argv))
