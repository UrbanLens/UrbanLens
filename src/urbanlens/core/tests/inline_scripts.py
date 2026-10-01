"""Checks shared by the tests that keep programs out of rendered pages.

An inline ``<script>`` cannot be cached between visits, cannot be minified by the bundler, and is invisible to the
TypeScript checks. Each one that carried a program has been moved to a static file fed by a JSON config element;
these helpers say whether such a move is complete and whether the config still matches what the script reads.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: What one inline block may still hold. A JSON island or a few lines of wiring is fine; a program is not.
MAX_INLINE_SCRIPT_BYTES = 20_000

#: An inline block: a ``<script>`` carrying its own body rather than a ``src``.
INLINE = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)
#: Every ``CFG.urls["x"]`` and ``CFG.assets["x"]`` a script reads.
INDEXED_READ = re.compile(r'CFG\.(urls|assets)\["([^"]+)"\]')
#: Every ``CFG.plainKey`` a script reads.
PLAIN_READ = re.compile(r"CFG\.([a-zA-Z][a-zA-Z0-9_]*)\b(?!\[)")
#: Any config read at all, indexed or plain.
ANY_READ = re.compile(r'CFG\.(?:urls|assets)\["[^"]+"\]|CFG\.[a-zA-Z][a-zA-Z0-9_]*')


def inline_blocks(page: bytes) -> list[bytes]:
    """Every inline script in *page*.

    Args:
        page: A rendered response body.

    Returns:
        The body of each ``<script>`` that has no ``src``."""
    return list(INLINE.findall(page))


def largest_inline(page: bytes) -> int:
    """The size of the biggest inline script in *page*.

    Args:
        page: A rendered response body.

    Returns:
        Bytes in the largest inline block, or 0 when there is none."""
    return max((len(block) for block in inline_blocks(page)), default=0)


def rendered_config(page: bytes, element_id: str) -> dict[str, Any] | None:
    """The JSON a page sends its script.

    Args:
        page: A rendered response body.
        element_id: The ``json_script`` element's id.

    Returns:
        The parsed configuration, or None when the element is absent."""
    pattern = re.compile(rb'<script[^>]*id="' + re.escape(element_id.encode()) + rb'"[^>]*>(.*?)</script>', re.DOTALL)
    match = pattern.search(page)
    if match is None:
        return None
    parsed: dict[str, Any] = json.loads(match.group(1).decode())
    return parsed


def missing_config(source: str, config: Mapping[str, Any]) -> list[str]:
    """Config the script reads that the page does not send.

    Args:
        source: The script's text.
        config: What the page rendered into its config element.

    Returns:
        The names of every read that has nothing behind it."""
    indexed = sorted(
        f"{group}.{key}" for group, key in set(INDEXED_READ.findall(source)) if key not in config.get(group, {})
    )
    plain = sorted(
        key for key in set(PLAIN_READ.findall(source)) if key not in {"urls", "assets"} and key not in config
    )
    return indexed + plain


def open_delimiter(source: str, position: int) -> str | None:
    """The string delimiter open at *position*, judged on its own line.

    Args:
        source: The script's text.
        position: An offset into it.

    Returns:
        The open quote character, or None when *position* is not inside a string."""
    index = source.rfind("\n", 0, position) + 1
    stack: str | None = None
    while index < position:
        character = source[index]
        if character == "\\":
            index += 2
            continue
        if character in "\"'`":
            stack = character if stack is None else (None if stack == character else stack)
        index += 1
    return stack


def inert_reads(source: str) -> list[str]:
    """Config reads that are quoted text rather than values.

    ``'history_v1_' + CFG.profileId`` keys one account's stored history; ``'history_v1_CFG.profileId'`` keys
    everyone's to the same string, and nothing raises to say so.

    Args:
        source: The script's text.

    Returns:
        The line of every read that a browser would treat as characters."""
    inert = []
    for match in ANY_READ.finditer(source):
        # Each file names its config differently, so the pattern matches the tail of a longer identifier
        # (``MAP_CFG.x``). Step back to where that identifier begins, or a ``${`` read looks like plain text.
        start = match.start()
        while start and (source[start - 1].isalnum() or source[start - 1] in "_$"):
            start -= 1
        delimiter = open_delimiter(source, start)
        if delimiter is None or (delimiter == "`" and source[start - 2 : start] == "${"):
            continue
        inert.append(source[source.rfind("\n", 0, start) + 1 : source.find("\n", match.end())].strip())
    return inert


def template_syntax(source: str) -> list[str]:
    """Django tags left in a file that is no longer a template.

    Args:
        source: The script's text.

    Returns:
        Each offending fragment; a tag named in a comment does not count."""
    code = re.sub(r"//[^\n]*", "", source)
    return [tag for tag in ("{%", "{{") if tag in code]
