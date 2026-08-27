"""OpenHistoricalMap plugin: rate limits plus the beta time-slider coverage panel.

Registers the OHM Overpass API's (deliberately conservative - see
``get_service_defaults``) rate-limit defaults and the one panel source that
checks whether OHM has dated coverage near a pin's location - see
``services.locations.temporal_imagery`` for the coverage panel itself and
``services.apis.locations.open_historical_map`` for why this integration is a
stopgap ahead of REData's own future temporal-imagery endpoint.
"""

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
                # This is public infrastructure run by volunteers with no
                # published rate limit beyond "2 concurrent request slots" and
                # a documented history of being overwhelmed - kept well under
                # that in both volume and pacing rather than testing the edge.
                calls_per_minute=10,
                calls_per_day=300,
                min_interval_seconds=1.5,
                notes="Free, keyless API. The volunteer-run instance publishes only a 2-concurrent-slot limit; kept deliberately conservative.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """The pin-detail coverage-check panel backing the time slider's visibility."""
        return [OhmTemporalCoveragePanelSource()]
