#!/usr/bin/env python3
"""Fail if a template writes a `/static/...` URL instead of going through `{% static %}`.

A literal path resolves, which is exactly what makes it hard to notice. What it
does not do is carry the content hash `ManifestStaticFilesStorage` puts in the
name - so the asset is served with `Cache-Control: max-age=60` where its hashed
sibling gets `max-age=315360000, immutable`, and a CDN edge re-fetches it every
minute forever. Four of these were live when this check was written, and one of
them was the 1.5 MB logo in the site header, on every authenticated page.

The second failure mode is worse and silent: a literal also ignores
`STATIC_URL`, so the day static assets move to their own hostname every one of
these keeps pointing at the app origin.

Not a defect, and skipped:

* **A comment.** A Django `{# ... #}` or an HTML `<!-- -->` recording a path -
  including a comment about this very check - is documentation, not a URL.
* **`{% static %}` output itself.** The tag's own argument never contains the
  prefix, so nothing this check looks for appears in a correct call.

Exits non-zero listing `file:line`. Safe to run by hand from the repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: Template roots to scan. Python code legitimately builds these paths (the
#: staticfiles finders, the checks themselves), so only templates are covered.
_TEMPLATE_GLOBS = ("src/urbanlens/**/templates/**/*.html",)

#: A literal reference to the static prefix, in any quoting style. The leading
#: slash is what makes it a URL rather than a `{% static %}` argument.
_LITERAL = re.compile(r"""["'=(]\s*/static/""")

#: Django comment, HTML comment - a path inside either is prose.
_COMMENT_LINE = re.compile(r"^\s*(?:\{#|<!--|#)")


def tracked_templates(root: pathlib.Path) -> list[pathlib.Path]:
    """Return every tracked template file.

    Args:
        root: Repository root.

    Returns:
        Paths to the template files git knows about, so an untracked scratch
        copy cannot fail the build.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", *_TEMPLATE_GLOBS],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / name for name in listed.stdout.split("\0") if name]


def offenders(paths: list[pathlib.Path], root: pathlib.Path) -> list[str]:
    """Find every literal `/static/` URL.

    Args:
        paths: Template files to read.
        root: Repository root, for reporting relative paths.

    Returns:
        ``file:line: text`` for each offending line, in file order.
    """
    found: list[str] = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if _COMMENT_LINE.match(line) or not _LITERAL.search(line):
                continue
            found.append(f"{path.relative_to(root)}:{number}: {line.strip()[:120]}")
    return found


def main() -> int:
    """Run the check.

    Returns:
        0 when every static reference goes through the tag, 1 otherwise.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    found = offenders(tracked_templates(root), root)
    if not found:
        return 0

    print("Templates writing a literal /static/ URL instead of {% static %}:")
    print("(a literal loses the content hash, so the asset is served max-age=60 rather than immutable,")
    print(" and it ignores STATIC_URL, so it breaks the day assets move to their own hostname)\n")
    for line in found:
        print(f"  {line}")
    print(f"\n{len(found)} literal reference(s). Add {{% load static %}} and use {{% static 'name' %}}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
