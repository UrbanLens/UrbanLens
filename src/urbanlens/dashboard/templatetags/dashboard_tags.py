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
def is_material_icon(value) -> bool:
    """Return True if value is a Material Icons name (ASCII letters/underscores only).

    Returns False for emoji or other Unicode characters, which are rendered as-is.

    Usage: {% if tag.icon|is_material_icon %}
    """
    return bool(value and re.match(r"^[a-z_]+$", str(value)))
