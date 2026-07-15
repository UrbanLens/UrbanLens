"""EPA ECHO plugin: nearby regulated-facility compliance panel. USA only."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class EpaEchoPanelSource(LocationCachePanelSource):
    """EPA-regulated facilities and their compliance status near the pin's location."""

    key = "epa_echo"
    cache_source = "epa_echo"
    section_id = "epa-echo-section"
    icon = "factory"
    title = "EPA Regulated Facilities"

    def fetch(self, pin: Pin) -> None:
        """Search EPA ECHO for nearby facilities and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.epa_echo import EpaEchoGateway
        from urbanlens.dashboard.services.geo_filter import is_usa_coordinates

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        if not is_usa_coordinates(lat, lng):
            LocationCache.set(pin.location, self.cache_source, {"facilities": []}, query_key=f"{lat:.5f},{lng:.5f}")
            return

        facilities = EpaEchoGateway().get_nearby_facilities(lat, lng, radius_miles=0.5, limit=10)
        LocationCache.set(pin.location, self.cache_source, {"facilities": facilities}, query_key=f"{lat:.5f},{lng:.5f}")


class EpaEchoPlugin(UrbanLensPlugin):
    """EPA ECHO regulated-facility compliance data for pinned locations. USA only."""

    name: ClassVar[str] = "epa_echo"
    verbose_name: ClassVar[str] = "EPA ECHO"
    description: ClassVar[str] = (
        "Free, keyless EPA Enforcement and Compliance History Online (ECHO) lookup - shows nearby "
        "regulated facilities and their compliance/violation status. USA only; strong urbex signal "
        "for industrial and contaminated sites."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the EPA ECHO REST API."""
        return {
            "epa_echo": ServiceDefaults(
                display_name="EPA ECHO",
                calls_per_minute=5,
                calls_per_day=500,
                usa_only=True,
                notes="Free, keyless API; observed to rate-limit aggressively under bursty use - kept conservative.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the EPA ECHO pin-detail panel."""
        return [EpaEchoPanelSource()]
