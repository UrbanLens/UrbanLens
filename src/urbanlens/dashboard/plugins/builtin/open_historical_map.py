"""OpenHistoricalMap plugin: rate limits plus the beta time-slider coverage panel."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.locations.temporal_imagery import OhmTemporalCoveragePanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class OpenHistoricalMapPlugin(UrbanLensPlugin):
    """Beta time-slider support: OpenHistoricalMap's dated OSM-derived vector data."""

    name: ClassVar[str] = "open_historical_map"
    verbose_name: ClassVar[str] = "OpenHistoricalMap"
    description: ClassVar[str] = "Dated roads/buildings/land-use from OpenHistoricalMap's Overpass API, powering the beta pin/wiki time slider."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the OHM Overpass API."""
        return {
            "open_historical_map": ServiceDefaults(
                display_name="OpenHistoricalMap Overpass API",
                calls_per_minute=10,
                calls_per_day=300,
                min_interval_seconds=1.5,
                notes="Free, keyless API. The volunteer-run instance publishes only a 2-concurrent-slot limit; kept deliberately conservative.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """The pin-detail coverage-check panel backing the time slider's visibility."""
        return [OhmTemporalCoveragePanelSource()]
