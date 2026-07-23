from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import operator
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.geo_filter import require_usa
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)


@dataclass(slots=True, kw_only=True)
class NPSGateway(Gateway):
    """Gateway for the National Park Service public API."""

    service_key: ClassVar[str] = "nps"
    paid_service: ClassVar[bool] = True

    api_key: str | None = settings.nps_api_key
    base_url: str = "https://developer.nps.gov/api/v1"

    def __post_init__(self):
        Gateway.__post_init__(self)
        if not self.api_key:
            raise ValueError("NPS API key must be provided.")

    @property
    def _headers(self) -> dict[str, str]:
        if self.api_key is None:
            raise RuntimeError("NPS API key is not configured")
        return {"X-Api-Key": self.api_key}

    def get_park_images(self, park_code: str) -> list:
        """
        Retrieve images for a specific park using the NPS API.

        Args:
            park_code: NPS park code (e.g. "yose" for Yosemite).

        Returns:
            List of image dicts.
        """
        if not park_code:
            raise ValueError("Park code must be provided to retrieve images.")

        params = {"parkCode": park_code}
        response = self.session.get(f"{self.base_url}/parks", headers=self._headers, params=params, timeout=60)
        response.raise_for_status()
        return self.handle_response(response, params)

    def search_parks(
        self,
        *,
        query: str = "",
        state_code: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Search NPS parks by name query and/or US state code.

        Args:
            query: Free-text search term (wiki/place name, etc.).
            state_code: Two-letter US state abbreviation (e.g. "NY").
            limit: Maximum number of parks to return.

        Returns:
            List of park data dicts. Each has at minimum: fullName, description,
            url, images (list), latLong, parkCode, states.
        """
        params: dict[str, Any] = {"limit": limit, "fields": "images,activities,operatingHours"}
        if query:
            params["q"] = query
        if state_code:
            params["stateCode"] = state_code.upper()

        try:
            resp = self.session.get(f"{self.base_url}/parks", headers=self._headers, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception:
            logger.exception("NPS park search failed (query=%r, state=%r)", query, state_code)
            return []

    def get_parks_near_location(
        self,
        latitude: float,
        longitude: float,
        state_code: str = "",
        radius_km: float = 100.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return NPS park units within *radius_km* of the given coordinates.

        Fetches parks by *state_code* (if provided) or all parks (capped at
        500), then filters to those whose centre point falls within the radius.
        Returns up to *limit* results sorted by distance.

        Args:
            latitude: Target latitude (WGS-84).
            longitude: Target longitude (WGS-84).
            state_code: Optional two-letter US state code to narrow the search.
            radius_km: Maximum distance to accept a park.
            limit: Maximum number of parks to return.

        Returns:
            List of place dicts compatible with the Places layer marker format.
            Each has: ``place_id``, ``name``, ``lat``, ``lng``, ``source``,
            ``description``, ``url``, ``types``, ``rating``, ``vicinity``.
        """
        if not require_usa("nps", latitude, longitude):
            return []

        parks = self.search_parks(state_code=state_code, limit=500)

        nearby: list[tuple[float, dict]] = []
        for park in parks:
            park_lat, park_lng = _parse_lat_long(park.get("latLong", ""))
            if park_lat is None or park_lng is None:
                continue
            dist = _haversine_km(latitude, longitude, park_lat, park_lng)
            if dist <= radius_km:
                nearby.append((dist, park))

        nearby.sort(key=operator.itemgetter(0))

        places = []
        for _dist, park in nearby[:limit]:
            park_lat, park_lng = _parse_lat_long(park.get("latLong", ""))
            places.append(
                {
                    "place_id": f"nps_{park.get('parkCode', '')}",
                    "name": park.get("fullName", ""),
                    "lat": park_lat,
                    "lng": park_lng,
                    "source": "nps",
                    "description": park.get("description", ""),
                    "url": park.get("url", ""),
                    "types": ["national_park"],
                    "rating": None,
                    "vicinity": park.get("states", ""),
                }
            )
        return places

    def get_park(self, park_code: str) -> dict[str, Any] | None:
        """Fetch full details for a single NPS park by its park code.

        Args:
            park_code: NPS park code (e.g. "yell" for Yellowstone).

        Returns:
            The park data dict (``fullName``, ``description``, ``images``,
            ``activities``, ``operatingHours``, ...), or None when the code
            matches no park or the request fails.
        """
        if not park_code:
            return None

        params: dict[str, Any] = {"parkCode": park_code, "limit": 1, "fields": "images,activities,operatingHours"}
        try:
            resp = self.session.get(f"{self.base_url}/parks", headers=self._headers, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return data[0] if data else None
        except Exception:
            logger.exception("NPS park lookup failed (park_code=%r)", park_code)
            return None

    def find_park_containing_location(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Return NPS details for the park whose boundary contains the point.

        Unlike a proximity search, this returns a park only when the
        coordinates fall *inside* an NPS unit's boundary -- so the pin-detail
        panel shows a park because the pinned place is in that park, not merely
        near it. Boundary containment is resolved server-side by
        :class:`NPSMapGateway`; the matched unit's rich detail is then fetched
        from the developer API.

        Args:
            latitude: Target latitude (WGS-84).
            longitude: Target longitude (WGS-84).

        Returns:
            The containing park's data dict, or None when the point is outside
            every NPS unit (or the boundary/detail lookup fails).
        """
        if not require_usa("nps", latitude, longitude):
            return None

        from urbanlens.dashboard.services.apis.parks.nps.map import NPSMapGateway

        try:
            park_code = NPSMapGateway().check_coordinates_within_park(latitude, longitude)
        except RateLimitExceededError:
            # Expected backpressure, not a bug: NPS's calls_per_minute budget is
            # tight (10/min) and this lookup runs unpaced on live pin-detail
            # views (unlike the background enrichment cycle, which paces
            # itself against the same budget) - a burst of concurrent views of
            # un-cached pins can legitimately exceed it. logger.warning (not
            # .exception) so it doesn't read as a crash - see GDELT's gateway
            # for the same convention on its own expected failures.
            logger.warning("NPS boundary lookup skipped - rate limit exceeded (near lat=%.2f, lng=%.2f)", latitude, longitude)
            return None
        except Exception:
            logger.exception("NPS boundary lookup failed (near lat=%.2f, lng=%.2f)", latitude, longitude)
            return None

        if not park_code:
            return None
        return self.get_park(park_code)

    def handle_response(self, response: requests.Response, request_data: dict[str, Any] | None = None) -> list:
        """
        Handle a response from the NPS API.

        Args:
            response: The HTTP response object.
            request_data: The original request parameters (for logging).

        Returns:
            List of image dicts, or empty list on error.
        """
        if not request_data:
            request_data = {}

        if getattr(response, "status_code", None) != 200:
            logger.error('Error getting images for %s -> Status Code: "%s"', request_data, response.status_code)
            return []

        try:
            body = response.json()
            return body.get("data", [])[0].get("images", [])

        except (json.JSONDecodeError, KeyError, IndexError):
            logger.exception("Error parsing json response for %s", request_data)
            return []


# -- helpers --------------------------------------------------------------------


def _parse_lat_long(lat_long_str: str) -> tuple[float | None, float | None]:
    """
    Parse the NPS ``latLong`` field (e.g. "lat:41.6032927, long:-101.8020015").

    Returns:
        (latitude, longitude) floats, or (None, None) on parse failure.
    """
    import re

    match = re.search(r"lat:([\-\d.]+).*?long:([\-\d.]+)", lat_long_str or "")
    if not match:
        return None, None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None, None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance in kilometres between two points."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))
