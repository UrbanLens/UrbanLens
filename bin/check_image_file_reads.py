#!/usr/bin/env python3
"""Fail if a template reads ``.image.url`` without first checking the file is there.

A row can exist with no stored file. ``Image.display_url``'s own docstring says
so - "an external gallery item whose download failed still carries its
``source_url``, and reading ``image.url`` on one of those raises" - and exists
to be used instead. Sixteen reads across eleven templates ignored it anyway, so
one such row returned a 500 for the whole panel rather than one missing
thumbnail: the visit history, the pin-share dialog, the DM bubble, the profile
photo strip, both cover heroes, and more.

Two shapes are fine, and getting either wrong would make this noise:

* **``display_url``/``thumb_url``.** They fall back rather than raise. This is
  what an ``Image`` should be read through.
* **A guard.** ``{% if comment.image %}`` around a read of ``comment.image.url``
  is correct and is what the comment attachments already do - those are the
  ``Comment`` model's own ``ImageField``, not an ``Image``, and have no
  ``display_url`` to reach for. Any enclosing ``{% if %}`` naming the same
  expression counts, however far above the read it sits.

Exits non-zero listing each unguarded read. Safe to run by hand from the repo
root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: Where the templates live, relative to the repo root.
_TEMPLATE_DIR = "src/urbanlens/dashboard/templates"

#: `<expr>.image.url`, capturing the object the file hangs off. Deliberately
#: matches anywhere in the line, not just inside `{{ }}`: the same read appears
#: in `{% include ... with x=y.image.url %}` and in inline script.
_READ = re.compile(r"([\w.]+)\.image\.url")

#: `{% if <expr>.image %}` - the guard that makes a read safe. Also accepts a
#: `{% with %}` binding of the same name, which a few list templates use.
#: Built per expression rather than as one pattern, since Django's own braces
#: rule out str.format here.
_GUARD_PREFIX = r"{%\s*(?:if|elif|with)\b[^%]*?\b"
_GUARD_SUFFIX = r"\.image\b(?!\.)"


def _tracked_templates(root: pathlib.Path) -> list[pathlib.Path]:
    """Every tracked template file, so an untracked scratch copy is ignored."""
    listing = subprocess.run(["git", "ls-files", _TEMPLATE_DIR], capture_output=True, text=True, check=True, cwd=root)
    return [root / line for line in listing.stdout.splitlines() if line.endswith(".html")]


def _unguarded(text: str) -> list[tuple[int, str]]:
    """Reads of ``.image.url`` in *text* with no guard anywhere above them.

    Args:
        text: One template's source.

    Returns:
        ``(line number, the expression read)`` for each unguarded read.
    """
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        for match in _READ.finditer(line):
            expr = match.group(1)
            guard = re.compile(_GUARD_PREFIX + re.escape(expr) + _GUARD_SUFFIX)
            if not any(guard.search(earlier) for earlier in lines[:number]):
                found.append((number, expr))
    return found


def main() -> int:
    """Report every unguarded read. Returns the process exit code."""
    root = pathlib.Path(__file__).resolve().parent.parent
    failures: list[str] = []
    for path in _tracked_templates(root):
        for number, expr in _unguarded(path.read_text(encoding="utf-8")):
            failures.append(f"{path.relative_to(root)}:{number}: {expr}.image.url is unguarded - use {expr}.display_url, or wrap it in {{% if {expr}.image %}}")

    if failures:
        print("\n".join(failures))
        print(f"\n{len(failures)} unguarded read(s). A photo row whose file never landed 500s the whole page.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
