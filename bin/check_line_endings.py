#!/usr/bin/env python3
"""Fail when a tracked text file would be stored with CRLF line endings."""

from __future__ import annotations

import subprocess
import sys

#: Index-side endings that must never appear on a text file. ``mixed`` and
#: ``cr`` are here because they are the same class of mistake as ``crlf``, not
#: because either has been seen in this repository.
_REJECTED = {"crlf", "mixed", "cr"}


def offending_files() -> list[tuple[str, str]]:
    """Find tracked text files stored with an ending other than LF.

    Returns:
        ``(path, index_ending)`` pairs, empty when the tree is clean.

    Raises:
        subprocess.CalledProcessError: If git itself fails, which should stop the commit rather than be reported as a clean tree."""
    result = subprocess.run(
        ["git", "ls-files", "--eol", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )

    offenders: list[tuple[str, str]] = []
    for record in result.stdout.split("\0"):
        if not record.strip():
            continue
        # "i/crlf  w/crlf  attr/text=auto eol=lf \t path/to/file"
        attrs, _, path = record.partition("\t")
        if not path:
            continue
        fields = attrs.split()
        index_eol = next((f[2:] for f in fields if f.startswith("i/")), "")
        # "-text" is git's marker for a file it classifies as binary; "none" is
        # a file with no line endings at all (a single unterminated line).
        if index_eol in _REJECTED:
            offenders.append((path.strip(), index_eol))
    return offenders


def main() -> int:
    """Report any tracked file stored with non-LF endings.

    Returns:
        0 when every tracked text file is LF, 1 otherwise.
    """
    try:
        offenders = offending_files()
    except subprocess.CalledProcessError as exc:
        print(f"check_line_endings: git ls-files failed ({exc.returncode})", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("check_line_endings: git not found on PATH", file=sys.stderr)
        return 1

    if not offenders:
        return 0

    print("Tracked files stored with non-LF line endings:")
    for path, ending in sorted(offenders):
        print(f"  {path}  (index: {ending})")
    print()
    print("This repository stores LF - see the comment at the top of .gitattributes.")
    print("A CRLF shebang alone is enough to make a container exit with a bare")
    print('"no such file or directory", and a whole-file ending flip conflicts')
    print("against every in-flight branch instead of against the lines that changed.")
    print()
    print("Fix with:  git add --renormalize <path>...   (then re-stage)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
