"""Open-Elevation plugin: free, open-source, keyless elevation lookups.

Contributes a simple pin-detail "Elevation" info panel; see
``services.apis.elevation.open_elevation`` for the gateway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource

#: Meters-to-feet, for showing both units without requiring a second lookup.
_METERS_PER_FOOT = 0.3048


class ElevationPanelSource(CoordinateGatedInfoPanelSource):
    """The pin's elevation above (or below) sea level."""

    key = "open_elevation"
    cache_source = "open_elevation"
    section_id = "open-elevation-section"
    icon = "landscape"
    title = "Elevation"

    def fetch(self, pin: Pin) -> None:
        """Look up the pin's elevation and cache it.

        A failed lookup is cached as an explicit empty result (see
        ``PanelSource.fetch``'s own contract) rather than left unfetched -
        Open-Elevation's public instance has no per-coordinate retry benefit
        worth polling for indefinitely.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.elevation.open_elevation import OpenElevationGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        elevation_m = OpenElevationGateway().get_elevation(lat, lng)
        LocationCache.set(pin.location, self.cache_source, {"elevation_m": elevation_m}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build a single quick-fact line from the cached elevation."""
        elevation_m = (data or {}).get("elevation_m")
        if elevation_m is None:
            return None

        elevation_ft = elevation_m / _METERS_PER_FOOT
        below_sea_level = elevation_m < 0
        text = f"{abs(elevation_m):,.0f} m ({abs(elevation_ft):,.0f} ft) {'below' if below_sea_level else 'above'} sea level"
        return {"facts": [{"icon": self.icon, "text": text}]}


class OpenElevationPlugin(UrbanLensPlugin):
    """Free, open-source, keyless elevation lookups."""

    name: ClassVar[str] = "open_elevation"
    verbose_name: ClassVar[str] = "Open-Elevation"
    description: ClassVar[str] = "Free, open-source, keyless elevation lookups (open-elevation.com) - shows the pin's elevation above/below sea level on the pin detail page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Open-Elevation public instance."""
        return {
            "open_elevation": ServiceDefaults(
                display_name="Open-Elevation",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless, open-source (self-hostable via the project's Docker image).",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the elevation pin-detail panel."""
        return [ElevationPanelSource()]
