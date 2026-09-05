#!/usr/bin/env python3
"""Build the Sphinx documentation, and fail if it produces no API reference.

``sphinx-build`` reports "build succeeded" for a configuration that emits three
pages and reads no docstring at all, which is what this repository shipped until
2026-09-05: ``docs/conf.py`` and ``docs/index.rst`` existed, nothing ran
``sphinx-apidoc``, no ``automodule`` directive was ever written, and the output
was ``index.html``, ``genindex.html`` and ``search.html``. Meanwhile ``CLAUDE.md``
justified its Google-docstring standard with "Sphinx consumes them".

So exit status is not the check. This asserts the build produced module pages,
which is the only claim anyone actually cares about, and prints how many.

Usage:
    bin/build_docs.py [--out DIR] [--strict]

``--strict`` turns Sphinx warnings into errors. Not the default: the Markdown in
this directory was written for GitHub, and MyST has opinions about some of it
that are not worth blocking a docs build over yet.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

#: Below this, something is wrong with `autoapi_dirs` rather than with the code.
#: The package had 900-odd documentable modules when this was written; the floor
#: is set low enough to survive a large deletion and high enough to catch the
#: failure it exists for, which produced zero.
_MINIMUM_API_PAGES = 100


def main() -> int:
    """Build the docs and report what the build actually produced."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="output directory (default: a temporary build/ under docs/)")
    parser.add_argument("--strict", action="store_true", help="turn Sphinx warnings into errors")
    args = parser.parse_args()

    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    source = root / "docs"
    out = pathlib.Path(args.out).resolve() if args.out else source / "_build" / "html"

    if out.exists():
        shutil.rmtree(out)

    # -j auto: the read phase dominates (a thousand generated API pages) and is
    # the part that parallelises. Serial, this build runs into the tens of minutes.
    command = [sys.executable, "-m", "sphinx", "-j", "auto", "-b", "html", str(source), str(out)]
    if args.strict:
        command.insert(-2, "-W")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        return result.returncode

    api_pages = sorted((out / "api").rglob("*.html")) if (out / "api").is_dir() else []
    if len(api_pages) < _MINIMUM_API_PAGES:
        print()
        print(f"The build succeeded and produced {len(api_pages)} API pages, fewer than the {_MINIMUM_API_PAGES} expected.")
        print("That is the failure this script exists for: sphinx-build exits 0 for a configuration")
        print("that reads no source at all. Check `autoapi_dirs` in docs/conf.py.")
        return 1

    print()
    print(f"docs built: {len(list(out.rglob('*.html')))} pages, {len(api_pages)} of them API reference -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
