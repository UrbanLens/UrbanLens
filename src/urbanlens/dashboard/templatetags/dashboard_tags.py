"""Custom template tags and filters for the dashboard app."""

from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def in_list(value, collection) -> bool:
    """Return True if value is found in collection.

    Usage: {{ value|in_list:some_set }}
    """
    return value in collection
