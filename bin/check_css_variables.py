#!/usr/bin/env python3
"""Fail if a stylesheet reads a custom property nothing defines.

`var(--ul-border)` against a token that does not exist is not a syntax error and
not a visible one either, because these are almost always written with a
fallback: `var(--border-color, #cbd5e1)` renders the fallback, forever, on every
theme. The rule looks themed, reviews as themed, and is a hard-coded colour - so
dark mode simply never reaches it. Ten files were in that state when this was
first measured, and eight references still were a month later.

Three kinds of use are *not* defects, and getting them wrong would make this
noise rather than a check:

* **Interpolated names.** `var(--ul-#{$name})` is a whole family of tokens; the
  name is not known until Sass runs. Skipped.
* **Runtime-set names.** `--tag-color` is written by TypeScript with
  `style.setProperty`, or by a template's inline `style="--x: ..."`. Those are
  real definitions in a place a stylesheet cannot declare them, so both are
  collected as definitions.
* **Names inside comments**, including a comment recording that a broken
  reference was removed - which is exactly the shape this check produces.

Exits non-zero listing each undefined property. Safe to run by hand from the
repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

#: Where the stylesheets live, relative to the repo root.
_SASS_DIR = "src/urbanlens/dashboard/frontend/sass"

#: Where a custom property can be *set* outside the stylesheets.
_RUNTIME_DIRS = ("src/urbanlens/dashboard/frontend/ts", "src/urbanlens/dashboard/templates")

#: `--name:` at the start of a declaration. A quote counts as an opener too:
#: a template sets one inline as `style="--label-color: ..."`.
_DEFINITION = re.compile(r"""(?:^|[;{'"])\s*(--[\w-]+)\s*:""", re.MULTILINE)

#: `var(--name`, capturing the name and whatever follows it, so an interpolated
#: `var(--ul-#{$k})` can be told from a plain one.
_USE = re.compile(r"var\(\s*(--[\w-]*)(.?)")

#: A custom-property name as a string literal anywhere in TypeScript. Broader
#: than `setProperty(` on purpose: the name is as often passed *to* a helper
#: that sets it (`positionAboveColliders(el, "--ul-undo-offset-y", ...)`) as
#: written at the call site. The cost is that a name only ever *read* by a test
#: string counts as defined, which is a false negative in the direction that
#: keeps this check quiet rather than wrong-but-loud.
_NAME_LITERAL = re.compile(r"""['"`](--[\w-]+)['"`]""")

#: A `//` comment, and a `/* */` block. Removed before scanning for uses, so a
#: comment naming a removed reference does not resurrect it.
_LINE_COMMENT = re.compile(r"(?<![:'\"])//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(text: str) -> str:
    """Return `text` with its `//` and `/* */` comments blanked out.

    Args:
        text: Stylesheet source.

    Returns:
        The same text with comment bodies removed, line structure intact.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def undefined_properties(sources: dict[str, str], runtime: dict[str, str]) -> dict[str, list[str]]:
    """Which custom properties are read but never set.

    Args:
        sources: Stylesheet path -> contents.
        runtime: Path -> contents for files that may set a property at runtime.

    Returns:
        Property name -> the stylesheet paths reading it, for each one nothing
        defines. Empty when every read resolves.
    """
    defined: set[str] = set()
    for text in sources.values():
        defined |= set(_DEFINITION.findall(text))
    for text in runtime.values():
        defined |= set(_NAME_LITERAL.findall(text))
        defined |= set(_DEFINITION.findall(text))

    undefined: dict[str, list[str]] = {}
    for path, text in sorted(sources.items()):
        for name, following in _USE.findall(strip_comments(text)):
            # `var(--ul-#{$kind})`: the name is assembled by Sass.
            if following == "#":
                continue
            if name not in defined:
                undefined.setdefault(name, []).append(path)
    return {name: sorted(set(paths)) for name, paths in undefined.items()}


def main() -> int:
    """Report every custom property a stylesheet reads and nothing defines."""
    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    tracked = [name for name in subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True, cwd=root).stdout.split("\0") if name]

    def read(names: list[str]) -> dict[str, str]:
        contents = {}
        for name in names:
            try:
                contents[name] = (root / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return contents

    sources = read([name for name in tracked if name.startswith(_SASS_DIR) and name.endswith(".scss")])
    runtime = read([name for name in tracked if name.startswith(_RUNTIME_DIRS) and name.endswith((".ts", ".tsx", ".html"))])

    if not sources:
        print(f"No stylesheets found under {_SASS_DIR}. This check would pass vacuously.")
        return 1

    undefined = undefined_properties(sources, runtime)
    if not undefined:
        return 0

    print(f"Custom properties read by a stylesheet and defined nowhere ({len(undefined)}):")
    for name, paths in sorted(undefined.items()):
        print(f"  {name}  <- {', '.join(paths)}")
    print()
    print("These are not visible failures: written with a fallback, as they usually are, the rule")
    print("renders the fallback on every theme - so it looks themed and is a hard-coded colour.")
    print("Point each at a real token in _tokens.scss, or define it there.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
