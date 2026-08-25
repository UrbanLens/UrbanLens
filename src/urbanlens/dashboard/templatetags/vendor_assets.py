"""Template access to the third-party asset table.

``{% vendor_asset "leaflet_js" %}`` writes the whole tag, and
``{% vendor_asset_url "leaflet_marker_icon" %}`` writes just a URL for the
assets referenced from script or CSS rather than by a tag of their own. Both
resolve through :mod:`services.core.vendor_assets`, so a version or a source is
changed in one place instead of in each template that mentions the library.
"""

from __future__ import annotations

from django import template

from urbanlens.dashboard.services.core.vendor_assets import vendor_asset_tag, vendor_asset_url

register = template.Library()


@register.simple_tag
def vendor_asset(key: str) -> str:
    """Render the ``<script>`` or ``<link>`` for a named third-party asset.

    Args:
        key: A key of ``VENDOR_ASSETS``.

    Returns:
        The tag, already marked safe.
    """
    return vendor_asset_tag(key)


@register.simple_tag
def vendor_asset_source(key: str) -> str:
    """The URL of a named third-party asset, for use inside script or CSS.

    Args:
        key: A key of ``VENDOR_ASSETS``.

    Returns:
        The URL the asset should be loaded from.
    """
    return vendor_asset_url(key)
