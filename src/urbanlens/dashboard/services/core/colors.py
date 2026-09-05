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
from typing import TypeGuard, overload

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


@overload
def clean_color(value: object, *, default: str, allow_none_keyword: bool = False) -> str: ...


@overload
def clean_color(value: object, *, default: None = None, allow_none_keyword: bool = False) -> str | None: ...


def clean_color(value: object, *, default: str | None = None, allow_none_keyword: bool = False) -> str | None:
    """Return ``value`` when it is a colour this application stores, else ``default``.

    The keyword-argument form used by the form and ``request.POST`` paths, where
    the caller decides what a missing or malformed colour falls back to. Built on
    :func:`is_hex_color` so there is one definition of what a colour is.

    Note:
        This overlaps :func:`sanitize_hex_color` and :func:`sanitize_optional_color`,
        which arrived independently on another branch for the JSON endpoints. They
        should converge on one API; until then both are kept because both have
        callers, and silently dropping either would loosen validation somewhere.

    Args:
        value: The raw submitted value, typically straight off ``request.POST``
            or a JSON body. Surrounding whitespace is ignored, which form posts
            routinely carry.
        default: What to return when ``value`` is missing, blank, or not a colour.
        allow_none_keyword: Permit the literal ``"none"`` (markup borders use it
            to mean "no border"). Off by default so it cannot leak into fields
            where it would be rendered as a CSS keyword by accident.

    Returns:
        A validated colour string, or ``default``. Every return is either
        ``default`` or a string, which is what the overloads above state: given a
        ``str`` default this never returns ``None``, so a caller assigning the
        result straight into a non-nullable column does not have to prove it.
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if allow_none_keyword and text.lower() == NO_COLOR:
        return NO_COLOR
    return text if is_hex_color(text) else default


class InvalidColorError(ValueError):
    """A submitted colour is present but is not one this application stores."""


def require_color(value: object, *, default: str | None = None, allow_none_keyword: bool = False) -> str | None:
    """Like :func:`clean_color`, but refuse a wrong value instead of replacing it.

    Missing and blank still fall back to ``default``: "unset" is not an invalid
    colour, and every caller here treats an absent key as "leave it alone". Only
    a value that is present, non-blank, and not a colour raises - which is the
    case where coercing loses information the caller supplied, tells them the
    write succeeded, and leaves them to discover the difference by reading the
    record back.

    Args:
        value: The raw submitted value, typically straight off a JSON body.
        default: What a missing or blank value falls back to.
        allow_none_keyword: Permit the literal ``"none"``.

    Returns:
        A validated colour string, or ``default``.

    Raises:
        InvalidColorError: When ``value`` is present and is not a colour.
    """
    if value is None or not str(value).strip():
        return default
    cleaned = clean_color(value, default=None, allow_none_keyword=allow_none_keyword)
    if cleaned is None:
        raise InvalidColorError(f"{str(value)[:64]!r} is not a colour. Use 6-digit hex, e.g. #1a2b3c.")
    return cleaned
