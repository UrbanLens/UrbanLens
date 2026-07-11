"""Nominatim plugin: OpenStreetMap place metadata panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class NominatimPanelSource(LocationCachePanelSource):
    """OpenStreetMap Nominatim place metadata for the pin's location."""

    key = "nominatim"
    cache_source = "nominatim"
    section_id = "nominatim-section"
    icon = "map"
    title = "OpenStreetMap"

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates and cache the place metadata."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        place = NominatimGateway().reverse_geocode(lat, lng)
        LocationCache.set(pin.location, self.cache_source, place or {}, query_key=f"{lat},{lng}")


class NominatimPlugin(UrbanLensPlugin):
    """OpenStreetMap place metadata for pinned locations."""

    name: ClassVar[str] = "nominatim"
    verbose_name: ClassVar[str] = "OpenStreetMap (Nominatim)"
    description: ClassVar[str] = "Reverse-geocodes pins via Nominatim and shows OpenStreetMap place metadata on the pin detail page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Nominatim API."""
        return {
            "nominatim": ServiceDefaults(
                display_name="Nominatim (OpenStreetMap)",
                calls_per_minute=1,
                calls_per_day=500,
                notes="Free API. Hard limit: 1 req/second per OSM ToS.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the OpenStreetMap pin-detail panel."""
        return [NominatimPanelSource()]
