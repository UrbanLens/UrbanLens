"""Panoramax plugin: free, open-source street-level imagery, via REData.

REData-backed (``services.apis.locations.redata_media_gateway`` - see that
module's docstring); no longer calls api.panoramax.xyz directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider


class PanoramaxPlugin(UrbanLensPlugin):
    """Panoramax crowdsourced street-level imagery in the street-view carousel."""

    name: ClassVar[str] = "panoramax"
    verbose_name: ClassVar[str] = "Panoramax"
    description: ClassVar[str] = "Free, open-source (GeoVisio) crowdsourced street-level imagery, backed by IGN - adds EU-strong coverage alongside the existing KartaView/Mapillary street-view providers. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Contribute the Panoramax street-view provider."""
        from urbanlens.dashboard.services.apis.locations.redata_media_gateway import PanoramaxStreetViewProvider

        return [PanoramaxStreetViewProvider()]
