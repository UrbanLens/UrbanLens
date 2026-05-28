"""Custom template tags and filters for the dashboard app."""

from __future__ import annotations

import re

from django import template

register = template.Library()


@register.filter
def in_list(value, collection) -> bool:
    """Return True if value is found in collection.

    Usage: {{ value|in_list:some_set }}
    """
    return value in collection


@register.filter
def tag_total_pins(tag) -> int:
    """Return direct pin count plus all direct children's pin counts.

    Requires prefetch_related('pins', 'children', 'children__pins') to avoid N+1.
    """
    total = tag.pins.count()
    for child in tag.children.all():
        total += child.pins.count()
    return total


@register.filter
def get_attr(obj, attr: str):
    """Return getattr(obj, attr), useful in loops over field names.

    Usage: {{ object|get_attr:field_name }}
    """
    return getattr(obj, attr, "")


@register.filter
def human_timesince(value) -> str:
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
def is_material_icon(value) -> bool:
    """Return True if value is a Material Icons name (ASCII letters/underscores only).

    Returns False for emoji or other Unicode characters, which are rendered as-is.

    Usage: {% if tag.icon|is_material_icon %}
    """
    return bool(value and re.match(r"^[a-z_]+$", str(value)))
