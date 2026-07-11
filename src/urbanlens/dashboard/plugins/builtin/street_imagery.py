"""Street-level imagery plugins: providers for the pin detail street carousel.

Plugin ``order`` values control carousel slide order (Google Street View,
defined in the ``google_maps`` module, is 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider


class MapillaryPlugin(UrbanLensPlugin):
    """Mapillary crowdsourced street-level imagery."""

    name: ClassVar[str] = "mapillary"
    verbose_name: ClassVar[str] = "Mapillary"
    description: ClassVar[str] = "Crowdsourced street-level imagery from Mapillary in the street view carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 25

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Mapillary API."""
        return {
            "mapillary": ServiceDefaults(
                display_name="Mapillary",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Requires a client access token from mapillary.com/dashboard/developers. Free tier available.",
            ),
        }

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute Mapillary street-level imagery."""
        from urbanlens.dashboard.services.apis.locations.mapillary import MapillaryGateway

        return [MapillaryGateway()]


class KartaViewPlugin(UrbanLensPlugin):
    """KartaView crowdsourced street-level imagery."""

    name: ClassVar[str] = "kartaview"
    verbose_name: ClassVar[str] = "KartaView"
    description: ClassVar[str] = "Crowdsourced street-level imagery from KartaView in the street view carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 35

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the KartaView API."""
        return {
            "kartaview": ServiceDefaults(
                display_name="KartaView",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Free, no key required. Crowdsourced street-level imagery.",
            ),
        }

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute KartaView street-level imagery."""
        from urbanlens.dashboard.services.apis.locations.kartaview import KartaViewGateway

        return [KartaViewGateway()]
