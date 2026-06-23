"""Custom template tags and filters for the dashboard app."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from django import template

if TYPE_CHECKING:
    from collections.abc import Collection
    import datetime

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
