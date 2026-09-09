"""The icon picker's grid markup, rendered once per process and served on demand.

``_icon_picker.html`` used to render every ``ICON_CATEGORIES`` entry inline, in
every picker on the page. One grid is 594,669 bytes and 2,498 buttons; the
achievement admin renders one per award plus one for its create form, so sixty
awards cost about 34.6 MB and 76,189 buttons, and Organize Labels renders
thirteen as a flat page cost. The catalogue is identical in all of them, so it
is fetched once by the browser rather than rendered N times by Django (P68).

Two properties make that safe to cache hard:

* the markup depends only on ``ICON_CATEGORIES`` and ``ICON_KEYWORDS``, both
  module constants, so it cannot vary by user, request or time; and
* it carries no picker id. The per-item handler reads the id from the enclosing
  ``.icon-picker-dropdown``, which is what lets one response serve every picker
  on the page and every page in the deployment.

:func:`icon_grid_version` hashes the rendered bytes, so the URL a template emits
changes exactly when the catalogue does. That is what allows an immutable
``Cache-Control`` without a deploy serving yesterday's icons.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib


@lru_cache(maxsize=1)
def icon_grid_html() -> str:
    """Render the shared grid of icon buttons.

    Cached for the life of the process: the inputs are module constants, and
    rendering 2,498 buttons is the cost this module exists to stop paying.

    Returns:
        The concatenated ``<button>`` markup, with no enclosing element.
    """
    from django.template.loader import render_to_string

    from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES

    return render_to_string("dashboard/partials/ui/_icon_picker_grid_items.html", {"icon_categories": ICON_CATEGORIES})


@lru_cache(maxsize=1)
def icon_grid_version() -> str:
    """A short content hash of :func:`icon_grid_html`, used to version its URL.

    Returns:
        Sixteen hex characters, stable for a given catalogue and changing with
        any edit to it.
    """
    return hashlib.sha256(icon_grid_html().encode("utf-8")).hexdigest()[:16]
