"""Validation for user-supplied colour values on the way into the database.

`Label.color` declares `choices` and `MarkupShape.color`/`border_color` declare nothing at
all, and in both cases Django enforces field `choices` only inside `full_clean()` - which
`Model.save()` does not call. Every colour write path assigns straight from request data, so
without this the stored value is simply "up to N characters of whatever was posted".

That matters because the browser interpolates these into `style="…"` attributes. The renderers
validate too (`frontend/ts/shared/color-safety.ts`, `markup-engine.safeColor`), but that is the
second line: a value that is not a colour has no business being stored, and a renderer added
later should not have to rediscover the rule.

Invalid input is coerced to the caller's default rather than raising. These values come from
palette pickers, so anything else is a malformed request rather than a user mistake worth
reporting, and the existing endpoints already treat a missing colour the same way.
"""

from __future__ import annotations

import re
from typing import overload

#: `#rgb` and `#rrggbb`. Everything the palettes offer is `#rrggbb`; the shorthand is
#: accepted because it is unambiguously a colour and costs nothing to allow.
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

#: Markup border colours use this sentinel to mean "draw no border", and the map renderer
#: checks for it by name - it is a meaningful value, not a missing one.
NO_COLOR = "none"


@overload
def clean_color(value: object, *, default: str, allow_none_keyword: bool = ...) -> str: ...


@overload
def clean_color(value: object, *, default: None = ..., allow_none_keyword: bool = ...) -> str | None: ...


def clean_color(value: object, *, default: str | None = None, allow_none_keyword: bool = False) -> str | None:
    """Return ``value`` when it is a colour this application stores, else ``default``.

    Args:
        value: The raw submitted value, typically straight off ``request.POST`` or a JSON body.
        default: What to return when ``value`` is missing, blank, or not a colour.
        allow_none_keyword: Permit the literal ``"none"`` (markup borders use it to mean
            "no border"). Off by default so it cannot leak into fields where it would be
            rendered as a CSS keyword by accident.

    Returns:
        A validated colour string, or ``default``.
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if allow_none_keyword and text.lower() == NO_COLOR:
        return NO_COLOR
    if HEX_COLOR_RE.match(text):
        return text
    return default
