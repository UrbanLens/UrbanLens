"""Mapbox gateway for satellite imagery and tile access."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, ClassVar, Protocol

from django.core.cache import cache
import requests

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_STATIC_BASE = "https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static"
_TILE_BASE = "https://api.mapbox.com/v4/mapbox.satellite"
_SATELLITE_CACHE_TTL = 30 * 24 * 3600


@dataclass(slots=True, kw_only=True)
class MapboxGateway(SatelliteViewProvider):
    """Gateway for Mapbox Static Images API and satellite tile access.

    Provides current high-resolution satellite imagery as ``SatelliteSlide``
    objects.  Images are fetched server-side so the access token is never
    exposed to the client.

    Requires: ``UL_MAPBOX_API_KEY`` — a Mapbox public access token (starts
    with ``pk.``).  Obtain one at https://account.mapbox.com/access-tokens/.
    """

    service_key: ClassVar[str] = "mapbox"
    paid_service: ClassVar[bool] = True

    api_key: str | None = field(default_factory=lambda: settings.mapbox_api_key)

    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 17,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        """Return a Mapbox satellite image as a SatelliteSlide.

        The image is fetched server-side and cached for 30 days so the access
        token is never sent to the browser.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            zoom: Tile zoom level (15-19 recommended; 17 gives ~street-block detail).
            width: Image width in pixels (max 1280; use 640 for @2x retina output).
            height: Image height in pixels (max 1280).

        Returns:
            List of ``SatelliteSlide`` with a ``data:`` URI image source, or ``None``
            when no access token is configured or the request fails.
        """
        if not self.api_key:
            return

        try:
            url = f"{_STATIC_BASE}/{longitude},{latitude},{zoom}/{width}x{height}@2x"
            resp = self.session.get(url, params={"access_token": self.api_key}, timeout=15)
            resp.raise_for_status()
            b64 = base64.b64encode(resp.content).decode("ascii")
        except requests.exceptions.RequestException as exc:
            logger.warning("Mapbox satellite image unavailable for %s, %s: %s", latitude, longitude, exc)
            return

        yield SatelliteSlide(
            img_src=f"data:image/jpeg;base64,{b64}",
            source="Mapbox Satellite",
            date="Current",
            detail="High resolution - current imagery",
        )

    def tile_url(self, z: int, x: int, y: int) -> str | None:
        """Return a Mapbox satellite tile URL for browser-side use.

        Args:
            z: Zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            Tile URL string including the access token, or ``None`` if no key
            is configured.
        """
        if not self.api_key:
            return None
        return f"{_TILE_BASE}/{z}/{x}/{y}@2x.jpg90?access_token={self.api_key}"
