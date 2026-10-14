#!/usr/bin/env python3
"""Build the Sphinx documentation, and fail if it produces no API reference."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

#: Below this, something is wrong with `autoapi_dirs` rather than with the code.
#: The package had 900-odd documentable modules when this was written; the floor
#: is set low enough to survive a large deletion and high enough to catch the
#: failure it exists for, which produced zero.
_MINIMUM_API_PAGES = 100

#: Sphinx read-phase workers. `-j auto` meant one per CPU the container could see (8 on the
#: Forgejo runner), each a fork of a ~3 GiB parent: the build peaked at 13.3 GiB inside a
#: 16 GiB docker-in-docker shared with the other job. Two workers peak at 6.1 GiB
#: (proportional set size of the process tree). SPHINX_JOBS overrides.
_DEFAULT_JOBS = "2"


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

    # The Sphinx stack is not a project dependency: it is supplied for this run. --frozen keeps uv
    # from rewriting uv.lock to add it. The read phase dominates (a thousand generated API pages) and
    # is the part that parallelises, but each worker costs about 1.2 GiB; see _DEFAULT_JOBS.
    jobs = os.environ.get("SPHINX_JOBS", _DEFAULT_JOBS)
    command = [
        "uv",
        "run",
        "--frozen",
        "--with",
        "sphinx~=9.1.0",
        "--with",
        "sphinx-autoapi~=3.8.1",
        "--with",
        "sphinx-rtd-theme~=3.1.0",
        "--with",
        "myst-parser~=5.1.0",
        "python",
        "-m",
        "sphinx",
        "-j",
        jobs,
        "-b",
        "html",
        str(source),
        str(out),
    ]
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
