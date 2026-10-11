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
        from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
        from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import (
            PARTIAL_MAPS_STALE_AFTER,
            RedataHistoricalMapsGateway,
            maps_answer_complete,
        )

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        try:
            maps = RedataHistoricalMapsGateway().get_maps_covering(lat, lng, limit=24)
        except LocationContextUnavailableError:
            return
        data: dict = {"maps": list(maps)}
        stale_after = None
        if not maps_answer_complete(maps):
            # REData's deadline cut off the nearby-maps read: show what it found, but ask again in minutes, not days.
            data[UNANSWERED_SOURCES_KEY] = ["nearby_maps"]
            stale_after = PARTIAL_MAPS_STALE_AFTER
        LocationCache.set(pin.location, self.cache_source, data, query_key=f"{lat:.5f},{lng:.5f}", stale_after=stale_after)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached map matches into gallery tiles. Sheets without a preview image are skipped.

        Args:
            data: This source's cached ``{"maps": [...]}``, each a ``/maps/`` match whose catalogue record is under ``sheet``.

        Returns:
            One tile per sheet with an image to show.
        """
        items = []
        for match in (data or {}).get("maps") or []:
            sheet = match.get("sheet") if isinstance(match, dict) else None
            if not isinstance(sheet, dict):
                continue
            service = _image_service(sheet)
            thumb_url = sheet.get("thumbnail_url") or (f"{service}/full/!400,400/0/default.jpg" if service else "")
            if not thumb_url:
                continue
            title = sheet.get("title") or "Historical map"
            if sheet.get("date_text"):
                title = f"{title} ({sheet['date_text']})"
            items.append(
                MediaItem(
                    url=f"{service}/full/!{_FULL_VIEW_PIXELS},{_FULL_VIEW_PIXELS}/0/default.jpg" if service else thumb_url,
                    thumb_url=thumb_url,
                    caption=title,
                    source=sheet.get("attribution") or "Historical map",
                    page_url=sheet.get("landing_page_url") or "",
                ),
            )
        return items


#: Longest edge of the scan the gallery's full view asks for. A whole sheet is tens of megapixels.
_FULL_VIEW_PIXELS = 1600


def _image_service(sheet: dict) -> str:
    """The sheet's IIIF image service base, or ``""`` when it has none.

    ``!w,h`` sizing reads the same in IIIF Image API 2 and 3, so the base serves either version.

    Args:
        sheet: REData's ``MapSheetSerializer`` row.

    Returns:
        The service URL without ``/info.json``.
    """
    info = str(sheet.get("iiif_info_url") or "")
    return info.removesuffix("/info.json") if info.endswith("/info.json") else ""


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
