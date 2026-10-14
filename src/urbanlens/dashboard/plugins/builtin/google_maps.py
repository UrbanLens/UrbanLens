"""Google Maps plugin: satellite and street-level imagery providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class GoogleMapsPlugin(UrbanLensPlugin):
    """Google Maps static satellite imagery and Street View."""

    name: ClassVar[str] = "google_maps"
    verbose_name: ClassVar[str] = "Google Maps Imagery"
    description: ClassVar[str] = "Google static satellite imagery and Street View in the pin detail carousels."
    author: ClassVar[str] = "UrbanLens"
    # First in both imagery carousels.
    order: ClassVar[int] = 10

    def setup(self, plugin: PluginContext) -> None:
        """Google static satellite and Street View providers, their rate-limit defaults, and background backfill of their photos."""
        from urbanlens.dashboard.plugins.points import ENRICHMENT_SOURCES, SATELLITE_PROVIDERS, SERVICE_DEFAULTS, STREET_VIEW_PROVIDERS
        from urbanlens.dashboard.services.photos.photo_enrichment import SatelliteEnrichmentSource, StreetViewEnrichmentSource

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "google_maps": ServiceDefaults(
                    display_name="Google Maps (Static/StreetView)",
                    category=EgressCategory.BILLED,
                    calls_per_minute=20,
                    calls_per_day=200,
                    # Static Maps and Street View Static are 10,000 a month free each; nothing else on the account calls either.
                    free_tier_per_calendar_month=10_000,
                    notes="Static Maps and Street View Static: 10,000 free a month each, then billed per call. The coverage probes before a Street View image are counted here too, though Google does not bill them.",
                ),
                "google_street_view_metadata": ServiceDefaults(
                    display_name="Google Street View Metadata",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=60,
                    calls_per_day=5000,
                    notes='Map right-click coverage probe. Free: "Street View Static API metadata requests are available at no charge. No quota is consumed" (developers.google.com/maps/documentation/streetview/metadata).',
                    billable=False,
                ),
            },
        )

        plugin.add(SATELLITE_PROVIDERS, self._gateway)

        plugin.add(STREET_VIEW_PROVIDERS, self._gateway)

        plugin.extend(ENRICHMENT_SOURCES, [StreetViewEnrichmentSource(), SatelliteEnrichmentSource()])

    def _gateway(self):
        """Build a gateway with the unrestricted API key."""
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
        from urbanlens.UrbanLens.settings.app import settings

        return GoogleMapsGateway(api_key=settings.google_unrestricted_api_key or "")
