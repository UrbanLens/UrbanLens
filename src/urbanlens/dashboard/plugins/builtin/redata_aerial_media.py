"""Aerial & drone footage plugin: a Media-gallery source for overhead views of a pin, via REData.

REData's ``/media/lookup/?is_aerial=true`` filters its pooled media index
down to drone and aerial footage, recognised from each item's own title and
description. An aerial view of a roofless mill or a fenced-off complex shows
what no street-level photo can, which makes this its own gallery tab rather
than rows mixed into the general media results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class AerialMediaSource(GalleryMediaSource):
    """Drone/aerial footage near the pin, as a Media-gallery tab."""

    key = "redata_aerial"
    cache_source = "redata_aerial"
    icon = "flight"
    title = "Aerial & Drone"

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates and REData - a coordinate lookup, not a name search."""
        return bool(pin.effective_latitude and pin.effective_longitude) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Look up aerial items near the pin via REData and cache them."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_media_gateway import RedataMediaGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        items = RedataMediaGateway().lookup(lat, lng, is_aerial=True, limit=24)
        LocationCache.set(pin.location, self.cache_source, {"items": items}, query_key=f"{lat:.5f},{lng:.5f}")

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached REData media rows into gallery tiles."""
        items = []
        for row in (data or {}).get("items") or []:
            url = row.get("url") or ""
            if not url:
                continue
            items.append(
                MediaItem(
                    url=url,
                    thumb_url=row.get("thumbnail_url") or "",
                    caption=row.get("title") or "Aerial view",
                    source=row.get("credit") or "Aerial",
                    page_url=row.get("page_url") or url,
                ),
            )
        return items

    def debug_count(self, data: dict) -> int:
        """Number of aerial items cached."""
        return len((data or {}).get("items") or [])


class AerialMediaPlugin(UrbanLensPlugin):
    """Aerial and drone footage for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_aerial_media"
    verbose_name: ClassVar[str] = "Aerial & Drone Footage"
    description: ClassVar[str] = "Adds an aerial/drone footage tab to the pin detail page's Media gallery, from REData's pooled media index filtered to overhead views."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the aerial-media gallery source."""
        return [AerialMediaSource()]
