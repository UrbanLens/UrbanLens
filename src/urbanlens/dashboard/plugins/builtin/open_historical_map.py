"""OpenHistoricalMap plugin: rate limits plus the beta time-slider coverage panel."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.locations.temporal_imagery import OhmTemporalCoveragePanelSource
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class OpenHistoricalMapPlugin(UrbanLensPlugin):
    """Beta time-slider support: OpenHistoricalMap's dated OSM-derived vector data."""

    name: ClassVar[str] = "open_historical_map"
    verbose_name: ClassVar[str] = "OpenHistoricalMap"
    description: ClassVar[str] = "Dated roads/buildings/land-use from OpenHistoricalMap's Overpass API, powering the beta pin/wiki time slider."
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """OpenHistoricalMap Overpass rate-limit defaults and the coverage-check panel that backs the time slider's visibility."""
        from urbanlens.dashboard.plugins.points import PANEL_SOURCES, SERVICE_DEFAULTS

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "open_historical_map": ServiceDefaults(
                    display_name="OpenHistoricalMap Overpass API",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=10,
                    calls_per_day=300,
                    min_interval_seconds=1.5,
                    notes="Free, keyless API. The volunteer-run instance publishes only a 2-concurrent-slot limit; kept deliberately conservative.",
                ),
            },
        )

        plugin.extend(PANEL_SOURCES, [OhmTemporalCoveragePanelSource()])
