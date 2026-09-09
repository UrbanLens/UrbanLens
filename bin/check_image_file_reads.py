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
#: matches anywhere, not just inside `{{ }}`: the same read appears in
#: `{% include ... with x=y.image.url %}` and in inline script.
_READ = re.compile(r"([\w.]+)\.image\.url")

#: `<expr>.image` or `<expr>.image.name` inside a tag - what a guard looks like.
#: `.name` is the other standard way to ask whether a FieldFile has a file, and
#: rejecting it would push authors to rewrite a correct guard to satisfy a lint.
#: `.url` is deliberately not accepted: testing it is what raises.
_GUARDED_EXPR = re.compile(r"\b([\w.]+)\.image(?:\.name)?\b(?!\.)")

#: Tags and reads together, so they can be walked in document order. DOTALL
#: because a guard is routinely written across several lines.
_SCAN = re.compile(
    r"(?P<tag>{%\s*(?P<tagname>\w+)(?P<tagbody>[^%]*?)%})|(?P<read>(?P<expr>[\w.]+)\.image\.url)",
    re.DOTALL,
)

#: Template comments. Stripped before scanning, so a read inside one - which is
#: never executed - is not reported, and a guard inside one does not protect.
_COMMENT_BLOCK = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL)
_COMMENT_LINE = re.compile(r"{#.*?#}", re.DOTALL)

#: Which tags open a scope a guard can live in, and which close one. Only these
#: three, so the stack stays aligned: no other block tag changes whether a guard
#: is in force, and pairing every `{% block %}`/`{% spaceless %}` would only add
#: ways to drift out of sync.
_OPENERS = {"if", "for", "with"}
_CLOSERS = {"endif": "if", "endfor": "for", "endwith": "with"}


def _blank_out(pattern: re.Pattern[str], text: str) -> str:
    """Replace each match with same-length whitespace, so line numbers survive."""
    return pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _tracked_templates(root: pathlib.Path) -> list[pathlib.Path]:
    """Every tracked template file, so an untracked scratch copy is ignored."""
    listing = subprocess.run(["git", "ls-files", _TEMPLATE_DIR], capture_output=True, text=True, check=True, cwd=root)
    return [root / line for line in listing.stdout.splitlines() if line.endswith(".html")]


def _unguarded(text: str) -> list[tuple[int, str]]:
    """Reads of ``.image.url`` that no enclosing tag guards.

    Enclosing, not merely earlier: a guard protects the block it opens and
    nothing else. Matching "anywhere above" instead lets an unrelated earlier
    loop that happens to reuse a variable name vouch for a later one - which is
    a plausible shape in a codebase with several similar `{% for %}` galleries,
    and would silently defeat the whole check.

    Args:
        text: One template's source.

    Returns:
        ``(line number, the expression read)`` for each unguarded read.
    """
    text = _blank_out(_COMMENT_LINE, _blank_out(_COMMENT_BLOCK, text))
    # One entry per open block: the expressions that block's own tag guards.
    # An `{% else %}` replaces the top entry, since the guard does not hold in
    # its own else-branch.
    stack: list[set[str]] = []
    found: list[tuple[int, str]] = []

    def report(expr: str, offset: int) -> None:
        if not any(expr in scope for scope in stack):
            found.append((text.count("\n", 0, offset) + 1, expr))

    for match in _SCAN.finditer(text):
        if match.group("tag"):
            name, body = match.group("tagname"), match.group("tagbody")
            # A tag can carry a read of its own: the pin page passes its cover
            # photo through `{% include ... with hero_image_url=... %}`, which
            # is where this whole class of bug was found. Checked against the
            # stack as it stands *before* the tag, since a tag cannot guard
            # itself.
            for read in _READ.finditer(body):
                report(read.group(1), match.start() + read.start())
            if name in _OPENERS:
                stack.append({expr.group(1) for expr in _GUARDED_EXPR.finditer(body)})
            elif name in _CLOSERS:
                if stack:
                    stack.pop()
            elif name in {"else", "elif"} and stack:
                stack[-1] = {expr.group(1) for expr in _GUARDED_EXPR.finditer(body)}
            continue

        report(match.group("expr"), match.start())
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
