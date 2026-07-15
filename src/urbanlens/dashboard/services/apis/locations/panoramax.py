"""Panoramax gateway for open, crowdsourced street-level imagery.

https://panoramax.fr / https://panoramax.xyz - a free, keyless, open-source
(GeoVisio) street-level imagery project backed by IGN (the French national
mapping agency), federating pictures across multiple public instances. Fills
the same role as the existing KartaView/Mapillary integrations with stronger
EU coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

import requests

from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider, StreetViewSlide
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

#: Panoramax's main public API instance (federates results from other public instances too).
_API_URL = "https://api.panoramax.xyz/api"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"


@dataclass(slots=True, kw_only=True)
class PanoramaxGateway(StreetViewProvider):
    """Gateway for the Panoramax STAC-style search API.

    No API key is required for read-only search.
    """

    service_key: ClassVar[str] = "panoramax"
    paid_service: ClassVar[bool] = False

    def __post_init__(self) -> None:
        """Attach a descriptive User-Agent, matching this project's other public-API gateways."""
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def search_near_coordinates(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 10) -> list[dict[str, Any]]:
        """Search for pictures near a coordinate, nearest first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in meters.
            limit: Maximum number of results (1-100).

        Returns:
            Raw STAC ``Feature`` dicts, distance-sorted by the API; empty on
            failure or when nothing is nearby.
        """
        params: dict[str, str | int] = {
            "place_position": f"{longitude},{latitude}",
            "place_distance": f"0-{max(1, int(radius))}",
            "limit": max(1, min(int(limit), 100)),
        }
        try:
            response = self.session.get(f"{_API_URL}/search", params=params, timeout=10)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("Panoramax search failed for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return []
        return body.get("features") or []

    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        """Yield nearby Panoramax pictures as StreetViewSlides.

        Images are loaded directly by the browser from the contributing
        instance's storage - no key to hide, so no server-side proxying.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in meters.

        Yields:
            StreetViewSlide entries, nearest first; nothing when no pictures
            are within range or the request fails.
        """
        for feature in self.search_near_coordinates(latitude, longitude, radius=radius, limit=limit):
            assets = feature.get("assets") or {}
            image_url = (assets.get("sd") or assets.get("hd") or {}).get("href")
            if not image_url:
                continue

            properties = feature.get("properties") or {}
            coordinates = (feature.get("geometry") or {}).get("coordinates") or [None, None]
            heading = properties.get("view:azimuth")

            yield StreetViewSlide(
                img_src=image_url,
                source="Panoramax",
                date=(properties.get("datetime") or "")[:10] or "Unknown",
                heading=float(heading) if heading is not None else None,
                latitude=coordinates[1],
                longitude=coordinates[0],
            )
