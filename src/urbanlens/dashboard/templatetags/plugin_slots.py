"""Render the template fragments plugins contribute to a page slot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

from urbanlens.dashboard.plugins.extensions import TemplateSlot, seam

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.template import Context

register = template.Library()


class UnknownSlotError(template.TemplateSyntaxError):
    """A template names a slot :mod:`~urbanlens.dashboard.plugins.points` does not declare."""


def render_slot(name: str, page_context: dict[str, Any], request: HttpRequest | None) -> SafeString:
    """
    Render every template contributed to slot ``name``, in priority order.

    Raises:
        UnknownSlotError: When no slot is declared under ``name``, so a misspelt slot fails loudly rather than
            rendering nothing forever.
    """
    declared = seam(name)
    if not isinstance(declared, TemplateSlot):
        raise UnknownSlotError(f"No template slot named {name!r} is declared in urbanlens.dashboard.plugins.points")
    # Each fragment is a template rendered with autoescaping, so joining them is safe.
    return mark_safe("".join(render_to_string(template_name, page_context, request=request) for template_name in declared))  # noqa: S308


@register.simple_tag(takes_context=True)
def plugin_slot(context: Context, name: str) -> SafeString:
    """Render every template plugins contribute to slot ``name`` with this page's context; nothing when none do."""
    # Template variables are named by strings; flatten() is typed to admit other keys, which no fragment could read.
    page_context = {key: value for key, value in context.flatten().items() if isinstance(key, str)}
    return render_slot(name, page_context, context.get("request"))


@register.simple_tag
def plugin_stylesheets() -> SafeString:
    """``<link>`` tags for the stylesheets plugins contribute to every page."""
    from django.templatetags.static import static
    from django.utils.html import format_html_join

    from urbanlens.dashboard.plugins.points import STYLESHEETS

    return format_html_join("\n", '<link rel="stylesheet" type="text/css" href="{}">', ((static(path),) for path in STYLESHEETS))


@register.simple_tag
def plugin_scripts() -> SafeString:
    """``<script type="module">`` tags for the scripts plugins contribute to every page."""
    from django.templatetags.static import static
    from django.utils.html import format_html_join

    from urbanlens.dashboard.plugins.points import SCRIPTS

    return format_html_join("\n", '<script type="module" src="{}"></script>', ((static(path),) for path in SCRIPTS))
