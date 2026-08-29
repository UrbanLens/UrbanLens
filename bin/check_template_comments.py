#!/usr/bin/env python3
"""Fail if a Django ``{# ... #}`` comment is not closed on the same line.

``{# #}`` is the single-line comment form. Django's lexer will not treat a
``{#`` that never meets a ``#}`` on that line as a comment, so the tokens are
emitted as text and the reader sees them in the page. The supported multi-line
form is ``{% comment %}...{% endcomment %}``.

The search this replaces is a regex for ``{#[^}]*$``: a ``{#`` that does not
meet a ``}`` before end-of-line. That both misses (a ``{{ var }}`` inside an
unclosed comment contains ``}``) and over-matches (a closed comment that
mentions a ``}``). This walks each line for ``{#`` / ``#}`` pairs instead.

``{% comment %}`` / ``{% verbatim %}`` regions are skipped: a ``{#`` inside one
is not rendered, which is the property this is guarding.

Exits non-zero listing each opening. Run by CI and pre-commit; safe to run by
hand from the repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: Django block tags that hide their body from rendering. Tags close with
#: ``%}`` or ``-%}``; the dash is before the percent, not after.
_BLOCK_HIDE = re.compile(
    r"\{%-?\s*comment\b.*?-?%\}.*?\{%-?\s*endcomment\s*-?%\}"
    r"|\{%-?\s*verbatim\b.*?-?%\}.*?\{%-?\s*endverbatim\s*-?%\}",
    re.DOTALL | re.IGNORECASE,
)


def _tracked_template_paths() -> list[pathlib.Path]:
    """Return committed Django templates (HTML plus ``templates/**/*.txt``)."""
    listing = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    paths: list[pathlib.Path] = []
    for raw in listing.stdout.splitlines():
        posix = raw.replace("\\", "/")
        if posix.endswith(".html"):
            paths.append(pathlib.Path(raw))
            continue
        if posix.endswith(".txt") and "/templates/" in posix:
            paths.append(pathlib.Path(raw))
    return paths


def _blank_hidden_blocks(source: str) -> str:
    """Replace ``{% comment %}`` / ``{% verbatim %}`` bodies with spaces.

    Newlines are kept so line numbers in the remainder still match the file.
    """

    def _keep_newlines(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _BLOCK_HIDE.sub(_keep_newlines, source)


def unclosed_hash_comment_lines(source: str) -> list[int]:
    """Return 1-based line numbers where a ``{#`` has no ``#}`` on that line.

    Args:
        source: Template source, including any ``{% comment %}`` regions.

    Returns:
        Sorted unique line numbers that open a hash-comment and do not close it
        before the newline.
    """
    visible = _blank_hidden_blocks(source)
    flagged: list[int] = []
    # ``str.splitlines()`` also splits on U+0085/U+2028/U+2029, which would
    # make a closed ``{# ... #}`` that contains one of those look unclosed.
    # Django's lexer does not treat them as line breaks.
    lines = visible.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for lineno, line in enumerate(lines, start=1):
        pos = 0
        while True:
            start = line.find("{#", pos)
            if start == -1:
                break
            end = line.find("#}", start + 2)
            if end == -1:
                flagged.append(lineno)
                break
            pos = end + 2
    return flagged


def check(paths: list[pathlib.Path] | None = None) -> int:
    """Report hash-comments that are not closed on the line they open.

    Args:
        paths: Templates to read. ``None`` means every committed template.

    Returns:
        Process exit code: non-zero when any such comment exists.
    """
    problems: list[str] = []
    scanned = 0
    for path in paths if paths is not None else _tracked_template_paths():
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path.as_posix()}: could not read ({exc})")
            continue
        for lineno in unclosed_hash_comment_lines(text):
            problems.append(f"{path.as_posix()}:{lineno}: {{# comment is not closed on this line - use {{% comment %}} for multi-line, or close with #}}")

    if problems:
        sys.stderr.write(f"Multi-line {{# template comments ({len(problems)}):\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write("\n{# #} is single-line. A {# that does not meet #} on the same line is rendered as text. Use {% comment %}...{% endcomment %} to span lines.\n")
        return 1

    print(f"All {scanned} templates close every {{# comment on the line it opens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
