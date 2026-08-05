"""Street-level imagery plugins: providers for the pin detail street carousel.

Plugin ``order`` values control carousel slide order (Google Street View,
defined in the ``google_maps`` module, is 10). Mapillary and KartaView are both
now REData-backed (``services.apis.locations.redata_media_gateway`` - see that
module's docstring); neither calls its upstream network directly any more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider


class MapillaryPlugin(UrbanLensPlugin):
    """Mapillary crowdsourced street-level imagery."""

    name: ClassVar[str] = "mapillary"
    verbose_name: ClassVar[str] = "Mapillary"
    description: ClassVar[str] = "Crowdsourced street-level imagery from Mapillary in the street view carousel, via REData."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 25

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute Mapillary street-level imagery."""
        from urbanlens.dashboard.services.apis.locations.redata_media_gateway import MapillaryStreetViewProvider

        return [MapillaryStreetViewProvider()]


class KartaViewPlugin(UrbanLensPlugin):
    """KartaView crowdsourced street-level imagery."""

    name: ClassVar[str] = "kartaview"
    verbose_name: ClassVar[str] = "KartaView"
    description: ClassVar[str] = "Crowdsourced street-level imagery from KartaView in the street view carousel, via REData."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 35

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute KartaView street-level imagery."""
        from urbanlens.dashboard.services.apis.locations.redata_media_gateway import KartaViewStreetViewProvider

        return [KartaViewStreetViewProvider()]
