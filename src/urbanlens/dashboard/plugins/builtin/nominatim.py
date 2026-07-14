"""Nominatim plugin: OpenStreetMap place metadata panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider


class NominatimPanelSource(LocationCachePanelSource):
    """OpenStreetMap Nominatim place metadata for the pin's location."""

    key = "nominatim"
    cache_source = "nominatim"
    section_id = "nominatim-section"
    icon = "map"
    title = "OpenStreetMap"

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates and cache the place metadata.

        Nominatim's panel data lands lazily (only once the pin detail page is
        viewed), well after a Location's ``official_name`` is first resolved
        at creation time. When that first resolution never found a real name
        - or, worse, fell back to a bare city/administrative name - this is
        the first opportunity to retry with OpenStreetMap's name in the mix,
        so the name is refreshed here rather than left stuck on a placeholder.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
        from urbanlens.dashboard.services.locations.naming import (
            is_address_derived_name,
            is_meaningful_name,
            update_location_name_from_external_sources,
        )

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        place = NominatimGateway().reverse_geocode(lat, lng)
        LocationCache.set(pin.location, self.cache_source, place or {}, query_key=f"{lat},{lng}")

        location = pin.location
        current_name = location.official_name
        name_needs_improvement = not is_meaningful_name(current_name) or bool(current_name and is_address_derived_name(current_name, location))
        if place and place.get("name") and name_needs_improvement:
            update_location_name_from_external_sources(location, profile=pin.profile)


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

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the reverse-geocoded OSM place name as a place-name candidate."""
        return [LocationCacheNameProvider(source="nominatim", cache_source="nominatim", keys=("name",), verbose_name="OpenStreetMap")]
