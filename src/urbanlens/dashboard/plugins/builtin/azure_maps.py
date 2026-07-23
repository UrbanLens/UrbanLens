"""Azure Maps plugin: reverse geocoding/POI panel, satellite imagery, and place names.

Azure Maps is Microsoft's actively-maintained geospatial platform - the
intended replacement for the now-legacy Bing Maps Imagery API this codebase
also integrates (see ``plugins.builtin.satellite_imagery`` /
``services.apis.locations.bing_maps``). One Azure Maps subscription key
authenticates every product area used here (Search, Geocoding, Render), all
wrapped by the gateways in ``services.apis.locations.azure``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider
    from urbanlens.dashboard.services.external_data import PanelSource


class AzureMapsPanelSource(LocationCachePanelSource):
    """Azure Maps reverse-geocoded address and nearest-POI data for a pin's location."""

    key = "azure_maps"
    cache_source = "azure_maps"
    section_id = "azure-maps-section"
    icon = "travel_explore"
    title = "Azure Maps"

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates and cache the nearest POI, if any.

        Two Azure Maps calls feed one cached payload: reverse geocoding for
        the formatted address/admin districts, and a tight-radius POI search
        for business-style details (category, phone, website) the geocoder
        alone doesn't return - mirroring how NominatimPanelSource combines
        both concerns into a single OpenStreetMap round trip. An empty result
        is cached explicitly when neither call finds anything, so the panel
        degrades to quietly absent rather than polling forever.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.azure.geocoding import AzureMapsGeocodingGateway
        from urbanlens.dashboard.services.apis.locations.azure.search import AzureMapsSearchGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)

        address = AzureMapsGeocodingGateway().reverse_geocode(lat, lng) or {}
        poi = AzureMapsSearchGateway().find_nearest_poi(lat, lng)

        payload = {**address, "poi": poi}
        LocationCache.set(pin.location, self.cache_source, payload, query_key=f"{lat:.5f},{lng:.5f}")


class AzureMapsNameProvider(NameProvider):
    """Place names from Azure Maps' nearest POI and reverse-geocoded address."""

    def __init__(self) -> None:
        """Initialize with the ``azure_maps`` source slug."""
        super().__init__(source="azure_maps", verbose_name="Azure Maps")

    def candidates(self, location: Location) -> list[str | None]:
        """Return the cached nearest-POI name and the reverse-geocoded locality.

        Args:
            location: The location to name.

        Returns:
            Raw candidate values; empty when no fresh Azure Maps cache row
            exists for this location.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cached = LocationCache.get_fresh(location, "azure_maps")
        data = cached.data if cached else None
        if not isinstance(data, dict):
            return []
        poi = data.get("poi") or {}
        return [poi.get("name"), data.get("name")]


class AzureMapsPlugin(UrbanLensPlugin):
    """Azure Maps geocoding, POI search, and satellite imagery for pinned locations."""

    name: ClassVar[str] = "azure_maps"
    verbose_name: ClassVar[str] = "Azure Maps"
    description: ClassVar[str] = (
        "Microsoft Azure Maps integration: reverse-geocoded address and nearby POI details on the pin detail page, static aerial/satellite imagery in the satellite carousel, and place-name candidates. Requires an Azure Maps subscription key."
    )
    author: ClassVar[str] = "UrbanLens"
    # Alongside the other satellite-imagery/name-resolution plugins (Google
    # Maps is 10, Google Places is 10); Azure sits just after them.
    order: ClassVar[int] = 15

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Azure Maps Search/Geocoding/Render APIs.

        All three product areas share one subscription key and one quota, so
        they share a single ``azure_maps`` service key/rate-limit row too.
        """
        return {
            "azure_maps": ServiceDefaults(
                display_name="Azure Maps (Search/Geocoding/Render)",
                calls_per_minute=50,
                calls_per_day=2500,
                notes="Free tier: 5,000 transactions/month (Gen1 S0 / Gen2 pay-as-you-go) shared across Search, Geocoding, and Render.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Azure Maps pin-detail panel."""
        return [AzureMapsPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute Azure Maps' POI/address names as place-name candidates."""
        return [AzureMapsNameProvider()]

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute Azure Maps static aerial/satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.azure.render import AzureMapsRenderGateway

        return [AzureMapsRenderGateway()]
