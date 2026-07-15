"""Azure Maps Render API: static map images, including satellite/aerial imagery."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, ClassVar

import requests

from urbanlens.dashboard.services.apis.locations.azure.gateway import AZURE_MAPS_BASE_URL
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider
from urbanlens.dashboard.services.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

#: Azure Maps Render (static image) API version this gateway targets.
#:
#: The newer ``tilesetId``-based Render v2 surface (``2024-04-01`` etc.)
#: rejects requests to this endpoint outright ("The specified API version is
#: not supported") on a live Azure Maps account tested against this code -
#: ``1.0`` is the legacy but still fully supported ``layer``/``style``-based
#: static image API, confirmed working.
RENDER_API_VERSION = "1.0"
#: Render API layer for satellite/aerial imagery with road/label overlay -
#: the static image API has no satellite-only layer, so this is the standard
#: way to get a "satellite view" image from it.
SATELLITE_LAYER = "hybrid"
#: Render API layer for the default road map style.
ROAD_LAYER = "basic"
#: The only Render v1 style; kept as a named constant rather than a literal
#: sprinkled through query params.
DEFAULT_STYLE = "main"


@dataclass(slots=True, kw_only=True)
class AzureMapsRenderGateway(SatelliteViewProvider):
    """Gateway for the Azure Maps Render (static map image) API.

    Provides current high-resolution aerial/satellite imagery as
    ``SatelliteSlide`` objects for the pin-detail satellite carousel, plus a
    general-purpose static map image method usable for any Azure Maps
    tileset/style. Images are fetched server-side so the subscription key is
    never exposed to the client.

    Can't subclass ``AzureMapsGateway`` (it must inherit
    ``SatelliteViewProvider`` instead), so it holds its own copy of
    ``subscription_key`` and calls the shared request helper directly - see
    ``services.apis.locations.azure.gateway`` for how to obtain a key.
    """

    service_key: ClassVar[str] = "azure_maps"
    paid_service: ClassVar[bool] = True

    subscription_key: str | None = field(default_factory=lambda: settings.azure_maps_subscription_key)

    def get_static_map_bytes(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 15,
        width: int = 640,
        height: int = 400,
        layer: str = ROAD_LAYER,
        style: str = DEFAULT_STYLE,
    ) -> bytes | None:
        """Fetch a static map image centered on a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            zoom: Map zoom level (0-20).
            width: Image width in pixels (max 1500).
            height: Image height in pixels (max 1500).
            layer: Render layer - ``"basic"`` for a roadmap, ``"hybrid"`` for
                satellite/aerial imagery with roads and labels, or
                ``"labels"`` for a labels-only overlay.
            style: Render style; ``"main"`` is the only value Render v1 supports.

        Returns:
            Raw PNG bytes, or None when no key is configured or the request
            fails.
        """
        if not self.subscription_key:
            return None

        params: dict[str, str | int] = {
            "subscription-key": self.subscription_key,
            "api-version": RENDER_API_VERSION,
            "layer": layer,
            "style": style,
            "center": f"{longitude},{latitude}",
            "zoom": zoom,
            "width": width,
            "height": height,
        }
        try:
            response = self.session.get(f"{AZURE_MAPS_BASE_URL}/map/static/png", params=params, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning("Azure Maps static map unavailable for %s, %s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return None

        return response.content

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
        """Yield a current Azure Maps aerial/satellite image as a SatelliteSlide.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.
            zoom: Map zoom level (17-18 is typically most useful for urbex).
            width: Image width in pixels.
            height: Image height in pixels.

        Yields:
            A SatelliteSlide with a ``data:`` URI image source; nothing when
            no key is configured or the request fails.
        """
        image_bytes = self.get_static_map_bytes(latitude, longitude, zoom=zoom, width=width, height=height, layer=SATELLITE_LAYER)
        if image_bytes is None:
            return

        b64 = base64.b64encode(image_bytes).decode("ascii")
        yield SatelliteSlide(
            img_src=f"data:image/png;base64,{b64}",
            source="Azure Maps",
            date="Current",
            detail="High resolution - current aerial imagery",
        )
