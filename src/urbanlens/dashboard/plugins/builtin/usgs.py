"""USGS plugin: historical topographic map panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class UsgsTopoPanelSource(LocationCachePanelSource):
    """USGS Historical Topographic Map Collection maps near the pin."""

    key = "usgs_topo"
    cache_source = "usgs_topo"
    section_id = "usgs-topo-section"
    icon = "terrain"
    title = "USGS Historical Topo Maps"

    def fetch(self, pin: Pin) -> None:
        """Query the TNM API for historical topo maps and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.usgs import UsgsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        result = UsgsGateway().historical_topo_maps_for_coordinates(lat, lng, delta=0.01)
        LocationCache.set(pin.location, self.cache_source, result or {}, query_key=f"{lat:.4f},{lng:.4f}")


class UsgsPlugin(UrbanLensPlugin):
    """USGS historical topographic maps for pinned locations."""

    name: ClassVar[str] = "usgs"
    verbose_name: ClassVar[str] = "USGS Historical Topo Maps"
    description: ClassVar[str] = "Shows USGS Historical Topographic Map Collection maps near a pin on the pin detail page. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the USGS APIs."""
        return {
            "usgs": ServiceDefaults(
                display_name="USGS EarthExplorer / TNM",
                calls_per_minute=10,
                calls_per_day=500,
                usa_only=True,
                notes="M2M requires an applicationToken from EarthExplorer account settings. TNM is fully public.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the USGS topo-map pin-detail panel."""
        return [UsgsTopoPanelSource()]
