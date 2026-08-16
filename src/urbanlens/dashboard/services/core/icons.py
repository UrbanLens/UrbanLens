"""Validation for user-supplied icon values on the way into the database.

``Pin.icon`` and ``Label.icon`` are plain ``CharField``s with no validator and no
choices, assigned straight from request data - exactly where colours were before
``services.core.colors.clean_color``. The field holds three different shapes
depending on which picker wrote it, and the renderers branch on that shape:

* a Material Icons name (``[a-z0-9_]+``), rendered as glyph text;
* a URL for an uploaded custom icon, rendered into ``<img src="...">``;
* an emoji, rendered as text.

The ``<img src>`` branch is the one that matters. The client half is already
covered (``_ulEscAttr`` in the map page, plus the ``^(https?://|/)`` test in
front of it), so this is the server half: a value that is not one of the three
shapes has no business being stored, and a renderer added later should not have
to rediscover the rule.

Invalid input is coerced to the caller's default rather than raising, matching
``clean_color``: these values come from icon pickers, so anything else is a
malformed request rather than a user mistake worth reporting.
"""

from __future__ import annotations

import re
from typing import overload
import unicodedata

#: Material Icons / Material Symbols names. Digits appear in several of them
#: (``filter_1``, ``3d_rotation``, ``9mp``), matching ``is_material_icon``.
MATERIAL_ICON_RE = re.compile(r"^[a-z0-9_]+$")

#: An uploaded icon's URL: absolute http(s), or a media/static-relative path.
#: Mirrors ``is_icon_url`` and the map marker builder's client-side test, which
#: is what keeps ``javascript:`` and ``data:`` out of the ``<img src>`` branch.
ICON_URL_RE = re.compile(r"^(?:https?://|/)[^\s\"'<>\\`]+$", re.IGNORECASE)

#: Longest stored icon value, from ``Pin.icon``/``Label.icon``.
MAX_ICON_LENGTH = 255

#: An emoji icon is a handful of code points (a base glyph plus modifiers,
#: variation selectors, or a ZWJ sequence), never a sentence.
MAX_EMOJI_CODEPOINTS = 12


def _is_emoji_token(text: str) -> bool:
    """Report whether ``text`` is plausibly a single emoji/short pictographic token.

    Args:
        text: A stripped candidate value.

    Returns:
        True when every code point is a non-ASCII symbol, mark, or joiner and
        the whole token is short enough to be one glyph rather than prose.
    """
    if len(text) > MAX_EMOJI_CODEPOINTS:
        return False
    for char in text:
        if char.isascii():
            return False
        # So/Sk/Cf cover pictographs, modifiers, ZWJ and variation selectors;
        # Mn covers combining marks. Letters (Lo, Ll, ...) are prose, not icons.
        if unicodedata.category(char) not in {"So", "Sk", "Sm", "Cf", "Mn"}:
            return False
    return True


@overload
def clean_icon(value: object, *, default: str) -> str: ...


@overload
def clean_icon(value: object, *, default: None = ...) -> str | None: ...


def clean_icon(value: object, *, default: str | None = None) -> str | None:
    """Return ``value`` when it is an icon this application stores, else ``default``.

    Args:
        value: The raw submitted value, typically straight off ``request.POST``
            or a JSON body.
        default: What to return when ``value`` is missing, blank, over-long, or
            not one of the three recognised shapes.

    Returns:
        A validated icon string, or ``default``.
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text or len(text) > MAX_ICON_LENGTH:
        return default
    if MATERIAL_ICON_RE.match(text):
        return text
    if ICON_URL_RE.match(text):
        return text
    if _is_emoji_token(text):
        return text
    return default
