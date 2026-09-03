#!/usr/bin/env python3
"""Fail when a tracked text file would be stored with CRLF line endings.

``.gitattributes`` already normalizes on the way into the index, so this is a
backstop rather than the mechanism - it catches the case where somebody adds an
attributes exception, or commits from a checkout whose attributes are stale.

Why a separate check rather than leaning on ``mixed-line-ending``: that hook
only sees paths matching the global ``files:`` pattern in
``.pre-commit-config.yaml``, and the whole reason this exists is that the
pattern listed ``yaml`` but not ``yml``, so ``docker-compose.yml`` was never
checked by anything. Widening that hook to cover *everything* is not the answer
either, because it rewrites what it is given and would corrupt a binary it was
handed by mistake. This one reads and reports.

The source of truth is ``git ls-files --eol``, which reports the ending as it
sits in the index and applies git's own text/binary classification - so a PNG
containing CRLF bytes is correctly ignored without maintaining a list of binary
extensions here.

Exit code 0 when clean, 1 with a per-file report otherwise.
"""

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
        subprocess.CalledProcessError: If git itself fails, which should stop
            the commit rather than be reported as a clean tree.
    """
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
