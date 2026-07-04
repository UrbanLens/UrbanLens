"""Bing Maps gateway for aerial (satellite) imagery."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.cache import cache
import requests

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_IMAGERY_URL = "https://dev.virtualearth.net/REST/V1/Imagery/Map/Aerial"
_METADATA_URL = "https://dev.virtualearth.net/REST/V1/Imagery/Metadata/Aerial"
_SATELLITE_CACHE_TTL = 30 * 24 * 3600


@dataclass(slots=True, kw_only=True)
class BingMapsGateway(SatelliteViewProvider):
    """Gateway for the Bing Maps REST Imagery API.

    Provides current high-resolution aerial (satellite) imagery as
    ``SatelliteSlide`` objects.  Images are fetched server-side so the API
    key is never exposed to the client.

    Requires: ``UL_BING_MAPS_API_KEY`` - a Bing Maps key from the Azure portal
    or https://www.bingmapsportal.com/.
    """

    service_key: ClassVar[str] = "bing_maps"
    paid_service: ClassVar[bool] = True

    api_key: str | None = field(default_factory=lambda: settings.bing_maps_api_key)

    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 18,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        """Return a Bing Maps aerial image as a SatelliteSlide.

        The image is fetched server-side and cached for 30 days.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            zoom: Map zoom level (1-20; 17-18 is typically most useful for urbex).
            width: Image width in pixels.
            height: Image height in pixels.

        Yields:
            List of ``SatelliteSlide`` with a ``data:`` URI image source, or ``None``
            when no API key is configured or the request fails.
        """
        if not self.api_key:
            return

        try:
            url = f"{_IMAGERY_URL}/{latitude},{longitude}/{zoom}"
            resp = self.session.get(
                url,
                params={"mapSize": f"{width},{height}", "format": "jpeg", "key": self.api_key},
                timeout=15,
            )
            resp.raise_for_status()
            b64 = base64.b64encode(resp.content).decode("ascii")
        except requests.exceptions.RequestException as exc:
            logger.warning("Bing Maps satellite image unavailable for %s, %s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return

        yield SatelliteSlide(
            img_src=f"data:image/jpeg;base64,{b64}",
            source="Bing Maps Aerial",
            date="Current",
            detail="High resolution - current imagery",
        )

    def get_tile_metadata(self, **params: Any) -> dict[str, Any]:
        """Return Bing Maps aerial tile metadata (URL templates, attributions, etc.).

        Useful for wiring the Bing tile layer into Leaflet or another slippy-map.

        Args:
            **params: Additional query parameters to pass to the REST API.

        Returns:
            Parsed JSON response from the Bing Maps Imagery Metadata API.

        Raises:
            ValueError: When no API key is configured.
        """
        if not self.api_key:
            raise ValueError("Bing Maps API key is not set. Set UL_BING_MAPS_API_KEY in .env.")
        response = self.session.get(_METADATA_URL, params={"key": self.api_key, **params}, timeout=10)
        response.raise_for_status()
        return response.json()
