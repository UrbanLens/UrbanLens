"""Find the files that own a WebSocket close handler, and read only their code.

A file that constructs its own `WebSocket` owns its `onclose` and must handle
the capacity refusal itself; a file that calls `openLiveSocket` inherits the
handling and must not have to repeat it. Discovering them by construction rather
than by a list is deliberate - the first fix for the refusal covered a third of
the clients because it was reasoned about rather than grepped for.

`executable_source` exists because every guarded file explains the close code in
a comment beside the branch that handles it, so a scan for the bare string
passes on a file that kept the comment and lost the branch.
"""

from __future__ import annotations

import pathlib
import re

#: `//` opens a comment unless it is the `//` of a scheme (`wss://`), which
#: would otherwise swallow the rest of the line - branch included.
_LINE_COMMENT = re.compile(r"(?<![:\w])//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TEMPLATE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DJANGO_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

#: Files that can carry one of these clients.
SOURCE_SUFFIXES = frozenset({".html", ".ts"})


def executable_source(text: str) -> str:
    """Return *text* with comments removed, so a scan reads only what runs."""
    for pattern in (_BLOCK_COMMENT, _TEMPLATE_COMMENT, _DJANGO_COMMENT, _LINE_COMMENT):
        text = pattern.sub("", text)
    return text


def hand_rolled(*roots: pathlib.Path) -> list[pathlib.Path]:
    """Every file under *roots* that constructs a WebSocket of its own.

    Args:
        roots: Directories to search.

    Returns:
        The matching paths, sorted, excluding test files (which construct fakes).
    """
    found = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in SOURCE_SUFFIXES or path.name.endswith(".test.ts"):
                continue
            if "new WebSocket(" in executable_source(path.read_text(encoding="utf-8", errors="ignore")):
                found.append(path)
    return found
