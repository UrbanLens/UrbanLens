"""Render the template fragments optional plugins contribute to a page slot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

if TYPE_CHECKING:
    from django.template import Context

register = template.Library()


@register.simple_tag(takes_context=True)
def plugin_slot(context: Context, name: str) -> SafeString:
    """
    Render every template a plugin contributes to slot ``name``, in plugin order, with this page's context.

    Args:
        context: The page's template context.
        name: The slot, as a plugin's ``get_template_slots`` names it.

    Returns:
        The rendered fragments, or an empty string when no plugin contributes to the slot.
    """
    from urbanlens.dashboard.plugins import plugin_registry

    page_context = context.flatten()
    request = context.get("request")
    return mark_safe("".join(render_to_string(template_name, page_context, request=request) for template_name in plugin_registry.template_slots().get(name, [])))  # noqa: S308
