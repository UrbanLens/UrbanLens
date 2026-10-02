"""Template access to the third-party asset table (versions/sources live in one place)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.utils.html import json_script

from urbanlens.dashboard.services.core.vendor_assets import leaflet_marker_artwork as marker_artwork, vendor_asset_tag

if TYPE_CHECKING:
    from django.utils.safestring import SafeString

register = template.Library()

#: The element ``map-layers.ts`` reads Leaflet's default marker artwork from.
LEAFLET_MARKER_ARTWORK_ID = "ul-leaflet-marker-artwork"


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
def leaflet_marker_artwork() -> SafeString:
    """Embed where Leaflet's default marker draws its images from, for ``map-layers.ts`` to hand to Leaflet.

    Returns:
        A ``<script type="application/json">`` block.
    """
    return json_script(marker_artwork(), LEAFLET_MARKER_ARTWORK_ID)


@register.simple_tag
def leaflet_marker_url(option: str) -> str:
    """The URL of one of Leaflet's marker images.

    Args:
        option: The ``L.Icon.Default`` option it fills: ``iconUrl``, ``iconRetinaUrl`` or ``shadowUrl``.

    Returns:
        The image's URL on this site.
    """
    return marker_artwork()[option]
