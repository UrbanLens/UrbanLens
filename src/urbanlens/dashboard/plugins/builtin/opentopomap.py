"""OpenTopoMap plugin: free, keyless, open-source topographic imagery."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider


class OpenTopoMapPlugin(UrbanLensPlugin):
    """OpenTopoMap topographic tiles in the satellite imagery carousel."""

    name: ClassVar[str] = "opentopomap"
    verbose_name: ClassVar[str] = "OpenTopoMap"
    description: ClassVar[str] = (
        "Free, keyless, open-source topographic map tiles (SRTM contours over OpenStreetMap) in the "
        "pin detail satellite carousel - trail/terrain context aerial photography doesn't show."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults - symbolic only; tiles load directly in the browser."""
        return {
            "opentopomap": ServiceDefaults(
                display_name="OpenTopoMap",
                calls_per_minute=None,
                calls_per_day=None,
                notes="Free, keyless tile server. No server-side calls to rate-limit - tiles load directly in the browser.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute the OpenTopoMap imagery slide."""
        from urbanlens.dashboard.services.apis.locations.opentopomap import OpenTopoMapGateway

        return [OpenTopoMapGateway()]
