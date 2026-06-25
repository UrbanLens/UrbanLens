"""Custom template tags and filters for the dashboard app."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from django import template
from django.utils.html import format_html, format_html_join

if TYPE_CHECKING:
    from collections.abc import Collection
    import datetime

    from django.utils.safestring import SafeString

    from urbanlens.dashboard.models.badges.model import Badge

register = template.Library()


@register.filter
def in_list(value: Any, collection: Collection[Any]) -> bool:
    """Return True if value is found in collection.

    Usage: {{ value|in_list:some_set }}
    """
    return value in collection


@register.filter
def tag_total_pins(tag: Badge) -> int:
    """Return direct pin count plus all direct children's pin counts.

    Uses annotated pin_count when available (set by BadgeQuerySet.with_pin_counts()).
    Falls back to DB queries only when annotations are absent.
    """
    total = getattr(tag, "pin_count", None)
    if total is None:
        total = tag.pins.count()
    for child in tag.children.all():
        child_count = getattr(child, "pin_count", None)
        total += child_count if child_count is not None else child.pins.count()
    return total


@register.filter
def get_attr(obj: Any, attr: str) -> Any:
    """Return getattr(obj, attr), useful in loops over field names.

    Usage: {{ object|get_attr:field_name }}
    """
    return getattr(obj, attr, "")


@register.filter
def human_timesince(value: datetime.datetime | datetime.date) -> str:
    """Return a human-friendly relative time string.

    Returns 'just now' for times less than 1 minute ago instead of '0 minutes ago'.

    Usage: {{ comment.created|human_timesince }}
    """
    from django.utils.timesince import timesince

    result = timesince(value)
    # timesince returns e.g. "0\xa0minutes" for < 1 min (non-breaking space between number and unit)
    if result.startswith("0"):
        return "just now"
    return f"{result} ago"


@register.filter
def is_material_icon(value: str | None) -> bool:
    """Return True if value is a Material Icons name (ASCII letters/underscores only).

    Returns False for emoji or other Unicode characters, which are rendered as-is.

    Usage: {% if tag.icon|is_material_icon %}
    """
    return bool(value and re.match(r"^[a-z_]+$", str(value)))


@register.filter
def icon_keywords(value: str | None) -> str:
    """Return space-separated search keywords for an emoji icon character.

    Looks up ``value`` in ``ICON_KEYWORDS`` and returns the associated string,
    or an empty string when no keywords are registered for this emoji.

    Usage: data-keywords="{{ tag.icon|icon_keywords }}"
    """
    from urbanlens.dashboard.models.badges.model import ICON_KEYWORDS

    return ICON_KEYWORDS.get(str(value), "")


@register.simple_tag
def tooltip_attrs(
    text: str,
    pos: str = "top",
    *,
    wide: bool = False,
    float_tip: bool = False,
) -> SafeString:
    """Return HTML attributes for a ``data-tooltip`` trigger.

    Usage::

        <button type="button" {% tooltip_attrs "Save your changes." pos="below" %}>

    Args:
        text: Tooltip copy shown on hover, focus, or tap.
        pos: Placement - ``top``, ``below``, ``left``, or ``right``.
        wide: Use a wider bubble for longer explanatory text.
        float_tip: Render via the floating JS layer (escapes ``overflow:hidden``).

    Returns:
        Safe HTML attribute string for the host element.
    """
    parts: list[SafeString] = [format_html('data-tooltip="{}"', text)]
    if pos and pos != "top":
        parts.append(format_html('data-tooltip-pos="{}"', pos))
    if wide:
        parts.append(format_html("data-tooltip-wide"))
    if float_tip:
        parts.append(format_html('data-tooltip-float="true"'))
    return format_html_join(" ", "{}", ((part,) for part in parts))


@register.inclusion_tag("dashboard/partials/_tooltip_help.html")
def tooltip_help(text: str, pos: str = "", wide: bool = False) -> dict[str, object]:
    """Render a standard info-icon tooltip trigger.

    Args:
        text: Tooltip copy.
        pos: Optional placement override.
        wide: Wider bubble for longer copy.

    Returns:
        Context dict for ``_tooltip_help.html``.
    """
    return {"text": text, "pos": pos, "wide": wide}


@register.inclusion_tag("dashboard/partials/_tooltip_label.html")
def tooltip_label(
    label: str,
    help_text: str = "",
    pos: str = "",
    wide: bool = False,
) -> dict[str, object]:
    """Render a label with an optional inline help tooltip.

    Args:
        label: Visible label text.
        help_text: Optional tooltip copy; omitted when empty.
        pos: Optional tooltip placement.
        wide: Wider bubble for longer copy.

    Returns:
        Context dict for ``_tooltip_label.html``.
    """
    return {"label": label, "help": help_text, "pos": pos, "wide": wide}
