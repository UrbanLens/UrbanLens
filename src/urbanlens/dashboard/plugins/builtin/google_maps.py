"""Google Maps plugin: satellite and street-level imagery providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider, StreetViewProvider


class GoogleMapsPlugin(UrbanLensPlugin):
    """Google Maps static satellite imagery and Street View."""

    name: ClassVar[str] = "google_maps"
    verbose_name: ClassVar[str] = "Google Maps Imagery"
    description: ClassVar[str] = "Google static satellite imagery and Street View in the pin detail carousels."
    author: ClassVar[str] = "UrbanLens"
    # First in both imagery carousels.
    order: ClassVar[int] = 10

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Google static imagery APIs."""
        return {
            "google_maps": ServiceDefaults(
                display_name="Google Maps (Static/StreetView)",
                calls_per_minute=20,
                calls_per_day=200,
                notes="Static Maps: 25,000 free/month. Street View: billed per call.",
            ),
        }

    def _gateway(self):
        """Build a gateway with the unrestricted API key."""
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
        from urbanlens.UrbanLens.settings.app import settings

        return GoogleMapsGateway(api_key=settings.google_unrestricted_api_key or "")

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute Google static satellite imagery."""
        return [self._gateway()]

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute Google Street View imagery."""
        return [self._gateway()]
