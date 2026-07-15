"""Azure Maps Search API: free-text fuzzy search and proximity POI search."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import requests

from urbanlens.dashboard.services.apis.locations.azure.gateway import AzureMapsGateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

#: Azure Maps Search API version this gateway targets.
SEARCH_API_VERSION = "1.0"


def _normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Search API result (address and/or POI) into a display-friendly dict."""
    poi = result.get("poi") or {}
    address = result.get("address") or {}
    position = result.get("position") or {}
    classifications = [entry.get("code") for entry in poi.get("classifications") or [] if entry.get("code")]

    return {
        "name": poi.get("name") or address.get("freeformAddress") or "",
        "categories": poi.get("categories") or [],
        "classifications": classifications,
        "phone": poi.get("phone") or "",
        "website": poi.get("url") or "",
        "address": address.get("freeformAddress") or "",
        "distance_meters": result.get("dist"),
        "latitude": position.get("lat"),
        "longitude": position.get("lon"),
        "entity_type": result.get("type") or "",
    }


@dataclass(slots=True, kw_only=True)
class AzureMapsSearchGateway(AzureMapsGateway):
    """Free-text and proximity POI search via the Azure Maps Search API."""

    def search(
        self,
        query: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        radius: int | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Free-text fuzzy search across addresses, POIs, and geographies.

        Args:
            query: Free-text search query.
            latitude: Optional bias/restrict-to latitude.
            longitude: Optional bias/restrict-to longitude.
            radius: Optional search radius in meters (used together with
                ``latitude``/``longitude``).
            limit: Maximum number of results (1-100).

        Returns:
            Normalized result dicts, most relevant first; empty when nothing
            matched or the request failed.

        Raises:
            ValueError: When no subscription key is configured.
        """
        if not query:
            return []
        params: dict[str, Any] = {"query": query, "limit": max(1, min(int(limit), 100))}
        if latitude is not None and longitude is not None:
            params["lat"] = latitude
            params["lon"] = longitude
            if radius is not None:
                params["radius"] = radius
        try:
            body = self._get("/search/fuzzy/json", api_version=SEARCH_API_VERSION, params=params)
        except requests.exceptions.RequestException:
            logger.warning("Azure Maps search failed for %r", query, exc_info=True)
            return []
        return [_normalize_result(result) for result in body.get("results") or []]

    def search_poi(
        self,
        latitude: float,
        longitude: float,
        *,
        query: str | None = None,
        radius: int = 1000,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search points of interest near a coordinate, nearest first.

        Args:
            latitude: WGS-84 latitude of the search origin.
            longitude: WGS-84 longitude of the search origin.
            query: Optional free-text filter (e.g. ``"coffee"``); omit to
                return every nearby POI regardless of category.
            radius: Search radius in meters.
            limit: Maximum number of results (1-100).

        Returns:
            Normalized POI dicts ordered by distance; empty when nothing was
            found nearby or the request failed.

        Raises:
            ValueError: When no subscription key is configured.
        """
        params: dict[str, Any] = {"lat": latitude, "lon": longitude, "radius": radius, "limit": max(1, min(int(limit), 100))}
        # "poi" takes a free-text query; "nearby" (no query param) ranks every
        # POI in range purely by distance - two distinct Search API endpoints.
        path = "/search/poi/json" if query else "/search/nearby/json"
        if query:
            params["query"] = query
        try:
            body = self._get(path, api_version=SEARCH_API_VERSION, params=params)
        except requests.exceptions.RequestException:
            logger.warning("Azure Maps POI search failed near %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return []
        return [_normalize_result(result) for result in body.get("results") or []]

    def find_nearest_poi(self, latitude: float, longitude: float, *, radius: int = 75) -> dict[str, Any] | None:
        """Find the single nearest POI to a coordinate - never by name.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in meters; kept tight so the match stays
                tied to the actual pinned building rather than a nearby,
                unrelated place.

        Returns:
            The nearest POI's normalized dict, or None when nothing is close
            enough.

        Raises:
            ValueError: When no subscription key is configured.
        """
        results = self.search_poi(latitude, longitude, radius=radius, limit=1)
        return results[0] if results else None
