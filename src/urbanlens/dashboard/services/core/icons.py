"""Validation for user-supplied icon values on the way into the database."""

from __future__ import annotations

from functools import lru_cache
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


@lru_cache(maxsize=1)
def _catalogue_icons() -> frozenset[str]:
    """Every icon the picker offers, as the authority on what is storable.
    Loosening the heuristic instead would have admitted the bare ASCII those entries are built on; a set cannot.

    Returns:
        The catalogue's icon values."""
    from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES

    return frozenset(icon for _label, pairs in ICON_CATEGORIES.values() for icon, _ in pairs)


def _is_emoji_token(text: str) -> bool:
    """Report whether ``text`` is plausibly a single emoji/short pictographic token.

    Args:
        text: A stripped candidate value.

    Returns:
        True when every code point is a non-ASCII symbol, mark, or joiner and the whole token is short enough to be one glyph rather than prose."""
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
def clean_icon(value: object, *, default: str, max_length: int = ...) -> str: ...


@overload
def clean_icon(value: object, *, default: None = ..., max_length: int = ...) -> str | None: ...


def clean_icon(value: object, *, default: str | None = None, max_length: int = MAX_ICON_LENGTH) -> str | None:
    """Return ``value`` when it is an icon this application stores, else ``default``.

    Args:
        value: The raw submitted value, typically straight off ``request.POST`` or a JSON body.
        default: What to return when ``value`` is missing, blank, over-long, or not one of the three recognised shapes.
        max_length: Longest accepted icon.

    Returns:
        A validated icon string, or ``default``."""
    if value is None:
        return default
    text = str(value).strip()
    if not text or len(text) > max_length:
        return default
    if text in _catalogue_icons():
        return text
    if MATERIAL_ICON_RE.match(text):
        return text
    if ICON_URL_RE.match(text):
        return text
    if _is_emoji_token(text):
        return text
    return default
