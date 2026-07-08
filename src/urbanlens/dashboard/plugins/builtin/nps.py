"""National Park Service plugin: nearby-park panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class NpsPanelSource(LocationCachePanelSource):
    """National Park Service information for the pin's location."""

    key = "nps"
    cache_source = "nps"
    section_id = "nps-section"
    icon = "park"
    title = "National Park Service"

    def fetch(self, pin: Pin) -> None:
        """Cache NPS details for the park the pin sits inside, if any.

        The panel is about the pinned place *being in* a national park, so the
        result comes from a boundary-containment check -- not a proximity
        search. When the pin falls outside every NPS unit an empty result is
        cached, which keeps the panel hidden.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        park = NPSGateway().find_park_containing_location(lat, lng)
        query_key = f"{lat:.5f},{lng:.5f}"
        LocationCache.set(location, self.cache_source, park or {}, query_key=query_key)


class NpsPlugin(UrbanLensPlugin):
    """National Park Service information for pinned locations."""

    name: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    description: ClassVar[str] = "Shows nearby US national park information on the pin detail page. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the NPS API."""
        return {
            "nps": ServiceDefaults(
                display_name="National Park Service API",
                calls_per_minute=10,
                calls_per_day=500,
                usa_only=True,
                notes="Free API. USA only - NPS covers US national parks exclusively.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the NPS pin-detail panel."""
        return [NpsPanelSource()]
