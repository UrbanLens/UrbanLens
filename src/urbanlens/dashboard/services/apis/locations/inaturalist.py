"""iNaturalist gateway - free, keyless nearby wildlife/plant observations.

https://api.inaturalist.org/v1/docs/ - a free, open-source (community
science) observation database. Useful "parks and recreation" context for
exploring a pinned outdoor location: what's actually been seen there.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_OBSERVATIONS_URL = "https://api.inaturalist.org/v1/observations"


def _normalize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Flatten one iNaturalist observation record into a display-friendly dict."""
    taxon = observation.get("taxon") or {}
    photo = taxon.get("default_photo") or {}
    return {
        "common_name": taxon.get("preferred_common_name") or "",
        "scientific_name": taxon.get("name") or "",
        "observed_on": observation.get("observed_on") or "",
        "photo_url": photo.get("square_url") or "",
        "uri": observation.get("uri") or "",
        "place_guess": observation.get("place_guess") or "",
        "iconic_taxon": taxon.get("iconic_taxon_name") or "",
    }


@dataclass(slots=True, kw_only=True)
class INaturalistGateway(Gateway):
    """Gateway for the iNaturalist public observations API. No API key required."""

    service_key: ClassVar[str] = "inaturalist"
    paid_service: ClassVar[bool] = False

    def get_nearby_observations(self, latitude: float, longitude: float, *, radius_km: float = 2, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent research-grade observations near a coordinate, nearest first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_km: Search radius in kilometers.
            limit: Maximum number of observations to return (1-200).

        Returns:
            Normalized observation dicts, nearest first; empty when nothing
            is nearby or the request fails.
        """
        params: dict[str, Any] = {
            "lat": latitude,
            "lng": longitude,
            "radius": radius_km,
            "per_page": max(1, min(int(limit), 200)),
            "order_by": "distance",
            "quality_grade": "research",
            "photos": "true",
        }
        try:
            response = self.session.get(_OBSERVATIONS_URL, params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("iNaturalist search failed for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return []
        return [_normalize_observation(observation) for observation in body.get("results") or []]
