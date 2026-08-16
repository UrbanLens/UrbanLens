"""Shared validation for user-supplied colours.

Markup colours are written by JSON endpoints that build models directly, and
are read back into ``innerHTML`` on the client (text-label spans, arrowhead
SVG), so an arbitrary string in one of these fields is a stored-XSS vector
rather than a cosmetic problem. Storage is therefore restricted to what the
renderers can actually mean: a 6-digit hex colour, plus the ``"none"`` sentinel
where a field is allowed to opt out of a border/background entirely.

These mirror ``safeColor``/``safeOptionalColor`` in
``dashboard/frontend/ts/shared/markup-engine.ts``, which validate the same
values again at render time - neither side assumes the other ran.
"""

from __future__ import annotations

import re
from typing import TypeGuard

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Sentinel meaning "no border / transparent background", not a colour.
NO_COLOR = "none"


def is_hex_color(value: object) -> TypeGuard[str]:
    """Whether ``value`` is a 6-digit ``#rrggbb`` string.

    A ``TypeGuard`` rather than a plain bool so the sanitizers below can return
    ``value`` directly in the true branch without the checker losing track of
    the ``isinstance`` that already happened here.

    Args:
        value: Any value; non-strings are simply not colours.

    Returns:
        True when ``value`` is a 6-digit hex colour.
    """
    return isinstance(value, str) and bool(HEX_COLOR_RE.match(value))


def sanitize_hex_color(value: object, fallback: str = "#e74c3c") -> str:
    """Return ``value`` when it is a hex colour, else ``fallback``.

    Args:
        value: The candidate colour, typically straight off a JSON body.
        fallback: What to use when ``value`` is not a usable colour.

    Returns:
        A 6-digit hex colour string.
    """
    return value if is_hex_color(value) else fallback


def sanitize_optional_color(value: object, fallback: str = "") -> str:
    """Return ``value`` when it is a hex colour or ``"none"``, else ``fallback``.

    Used for fields where "unset" (``""``) and "explicitly no colour"
    (``"none"``) are both meaningful, such as ``PinMarkup.border_color``.

    Args:
        value: The candidate colour, typically straight off a JSON body.
        fallback: What to use when ``value`` is neither a colour nor ``"none"``;
            defaults to the empty "unset" value.

    Returns:
        A 6-digit hex colour, ``"none"``, or ``fallback``.
    """
    if value == NO_COLOR:
        return NO_COLOR
    return value if is_hex_color(value) else fallback
