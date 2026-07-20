"""Panoramax plugin: free, keyless, open-source street-level imagery."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider


class PanoramaxPlugin(UrbanLensPlugin):
    """Panoramax crowdsourced street-level imagery in the street-view carousel."""

    name: ClassVar[str] = "panoramax"
    verbose_name: ClassVar[str] = "Panoramax"
    description: ClassVar[str] = "Free, keyless, open-source (GeoVisio) crowdsourced street-level imagery, backed by IGN - adds EU-strong coverage alongside the existing KartaView/Mapillary street-view providers."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Panoramax public API."""
        return {
            "panoramax": ServiceDefaults(
                display_name="Panoramax",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless public API (api.panoramax.xyz).",
            ),
        }

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute the Panoramax street-view provider."""
        from urbanlens.dashboard.services.apis.locations.panoramax import PanoramaxGateway

        return [PanoramaxGateway()]
