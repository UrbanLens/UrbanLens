"""Historical map sheets as Photos-tab tiles, from REData's spatial index."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class HistoricalMapMediaSource(GalleryMediaSource):
    """Georeferenced historical maps covering the pin, shown in the Photos gallery."""

    key = "historical_maps"
    cache_source = "historical_maps"
    icon = "map"
    title = "Historical Maps"

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates and REData."""
        return bool(pin.effective_latitude and pin.effective_longitude) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Cache the map sheets covering this pin."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
        from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        try:
            maps = RedataHistoricalMapsGateway().get_maps_covering(lat, lng, limit=24)
        except LocationContextUnavailableError:
            maps = []
        LocationCache.set(pin.location, self.cache_source, {"maps": maps}, query_key=f"{lat:.5f},{lng:.5f}")

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached map matches into gallery tiles. Sheets without a preview image are skipped."""
        items = []
        for row in (data or {}).get("maps") or []:
            image_url = row.get("thumbnail_url") or row.get("image_url") or ""
            if not image_url:
                continue
            title = row.get("title") or "Historical map"
            if row.get("date_text"):
                title = f"{title} ({row['date_text']})"
            items.append(
                MediaItem(
                    url=image_url,
                    thumb_url=image_url,
                    caption=title,
                    source=row.get("attribution") or "Historical map",
                    page_url=row.get("landing_page_url") or "",
                ),
            )
        return items


class HistoricalMapMediaPlugin(UrbanLensPlugin):
    """Historical map sheets in the Photos gallery."""

    name: ClassVar[str] = "redata_historical_map_media"
    verbose_name: ClassVar[str] = "Historical Maps"
    description: ClassVar[str] = "Shows georeferenced historical maps covering a pin in the Photos gallery."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """No extra quota: map lookups share the REData gateway already registered elsewhere."""
        return {}

    def get_panel_sources(self) -> list[PanelSource]:
        """The Photos-tab map source."""
        return [HistoricalMapMediaSource()]
