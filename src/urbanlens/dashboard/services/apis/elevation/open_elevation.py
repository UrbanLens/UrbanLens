"""Open-Elevation gateway - free, open-source, keyless elevation lookups.

https://open-elevation.com/ - a public, self-hostable elevation API (SRTM/
ASTER-derived global DEM). No API key required.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

#: Open-Elevation's public instance; self-hostable per the project's own docs
#: (a Docker image with the same REST API is published for production use).
_LOOKUP_URL = "https://api.open-elevation.com/api/v1/lookup"


@dataclass(slots=True, kw_only=True)
class OpenElevationGateway(Gateway):
    """Gateway for the free, keyless Open-Elevation API."""

    service_key: ClassVar[str] = "open_elevation"
    paid_service: ClassVar[bool] = False

    def get_elevation(self, latitude: float, longitude: float) -> float | None:
        """Return the elevation in meters at a single coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Elevation in meters, or None when the request failed.
        """
        results = self.get_elevations([(latitude, longitude)])
        return results[0] if results else None

    def get_elevations(self, coordinates: list[tuple[float, float]]) -> list[float | None] | None:
        """Return elevations in meters for a batch of coordinates, in order.

        Args:
            coordinates: List of ``(latitude, longitude)`` pairs.

        Returns:
            One elevation (meters) per input coordinate in the same order,
            or None when the whole request failed.
        """
        if not coordinates:
            return []
        locations = "|".join(f"{latitude},{longitude}" for latitude, longitude in coordinates)
        try:
            response = self.session.get(_LOOKUP_URL, params={"locations": locations}, timeout=20)
            response.raise_for_status()
            results = response.json().get("results") or []
        except requests.exceptions.RequestException:
            logger.warning("Open-Elevation lookup failed for %d coordinate(s)", len(coordinates), exc_info=True)
            return None
        by_index = {index: result.get("elevation") for index, result in enumerate(results)}
        if len(coordinates) == 1 and not results:
            logger.debug("Open-Elevation returned no result for %s, %s", redact_coordinate(coordinates[0][0]), redact_coordinate(coordinates[0][1]))
        return [by_index.get(index) for index in range(len(coordinates))]
