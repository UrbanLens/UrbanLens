"""The icon picker's grid markup, rendered once per process and served on demand. * the markup depends only on ``ICON_CATEGORIES`` and ``ICON_KEYWORDS``, both module constants, so it cannot vary by user, request or time; and * it carries no picker id."""

from __future__ import annotations

from functools import lru_cache
import hashlib


@lru_cache(maxsize=1)
def icon_grid_html() -> str:
    """Render the shared grid of icon buttons.

    Returns:
        The concatenated ``<button>`` markup, with no enclosing element."""
    from django.template.loader import render_to_string

    from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES

    return render_to_string("dashboard/partials/ui/_icon_picker_grid_items.html", {"icon_categories": ICON_CATEGORIES})


@lru_cache(maxsize=1)
def icon_grid_version() -> str:
    """A short content hash of :func:`icon_grid_html`, used to version its URL.

    Returns:
        Sixteen hex characters, stable for a given catalogue and changing with
        any edit to it."""
    return hashlib.sha256(icon_grid_html().encode("utf-8")).hexdigest()[:16]
