"""Google Maps plugin: satellite and street-level imagery providers."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider, StreetViewProvider
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource


class GoogleMapsPlugin(UrbanLensPlugin):
    """Google Maps static satellite imagery and Street View."""

    name: ClassVar[str] = "google_maps"
    verbose_name: ClassVar[str] = "Google Maps Imagery"
    description: ClassVar[str] = "Google static satellite imagery and Street View in the pin detail carousels."
    author: ClassVar[str] = "UrbanLens"
    # First in both imagery carousels.
    order: ClassVar[int] = 10

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Google static imagery APIs and the Street View coverage probe."""
        return {
            "google_maps": ServiceDefaults(
                display_name="Google Maps (Static/StreetView)",
                calls_per_minute=20,
                calls_per_day=200,
                notes="Static Maps: 25,000 free/month. Street View: billed per call.",
            ),
            "google_street_view_metadata": ServiceDefaults(
                display_name="Google Street View Metadata",
                calls_per_minute=60,
                calls_per_day=5000,
                notes='Map right-click coverage probe. Free: "Street View Static API metadata requests are available at no charge. No quota is consumed" (developers.google.com/maps/documentation/streetview/metadata).',
                cost_per_call=Decimal(0),
                billable=False,
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

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute static Street View/satellite photo backfill to scheduled background enrichment."""
        from urbanlens.dashboard.services.photos.photo_enrichment import SatelliteEnrichmentSource, StreetViewEnrichmentSource

        return [StreetViewEnrichmentSource(), SatelliteEnrichmentSource()]
