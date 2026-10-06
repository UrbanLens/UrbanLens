"""Aerial & drone footage plugin: a Media-gallery source for overhead views of a pin, via REData.
An aerial view of a roofless mill or a fenced-off complex shows what no street-level photo can, which makes this its own gallery tab rather than rows mixed into the general media results."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
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
        """Cache the aerial rows of the point's shared REData media answer, which the Nearby Media tab also reads."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.redata_nearby_media import stored_media_row
        from urbanlens.dashboard.services.locations.redata_point_data import media_near, point_key

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = media_near(lat, lng)
        items = [stored_media_row(row) for row in envelope.results if row.get("is_aerial")]
        LocationCache.set(pin.location, self.cache_source, envelope.marked({"items": items}), query_key=point_key(lat, lng))

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached REData media rows into gallery tiles, mirrored images read through this site's proxy."""
        from urbanlens.dashboard.plugins.builtin.redata_nearby_media import media_item_from_row

        return [item for row in (data or {}).get("items") or [] if isinstance(row, dict) and (item := media_item_from_row(row, fallback_caption="Aerial view")) is not None]


class AerialMediaPlugin(UrbanLensPlugin):
    """Aerial and drone footage for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_aerial_media"
    verbose_name: ClassVar[str] = "Aerial & Drone Footage"
    description: ClassVar[str] = "Adds an aerial/drone footage tab to the Private Pin page's Media gallery, from REData's pooled media index filtered to overhead views."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for redata_media."""
        return {
            "redata_media": ServiceDefaults(
                display_name="REData Media",
                category=EgressCategory.REDATA,
                calls_per_minute=20,
                calls_per_day=None,
                notes="Pooled media index lookups via GET /media/lookup/, filtered here to aerial and drone footage. Shares REData's one 1,000/hour lookup pool per key. See services.apis.locations.redata_media_gateway.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the aerial-media gallery source."""
        return [AerialMediaSource()]
