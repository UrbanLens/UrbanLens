"""USGS Earthquake Hazards gateway - free, keyless seismic activity lookups.

https://earthquake.usgs.gov/fdsnws/event/1/ - the USGS FDSN event catalog,
covering global seismicity. No API key is required. Useful safety context for
exploring older/abandoned structures: recent nearby seismic activity is a
real structural-risk signal this project's users should be able to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"


def _normalize_event(feature: dict[str, Any]) -> dict[str, Any]:
    """Flatten one FDSN GeoJSON earthquake feature into a display-friendly dict."""
    properties = feature.get("properties") or {}
    time_ms = properties.get("time")
    occurred_at = datetime.fromtimestamp(time_ms / 1000, tz=UTC).isoformat() if isinstance(time_ms, (int, float)) else ""
    return {
        "magnitude": properties.get("mag"),
        "place": properties.get("place") or "",
        "occurred_at": occurred_at,
        "url": properties.get("url") or "",
    }


@dataclass(slots=True, kw_only=True)
class UsgsEarthquakeGateway(Gateway):
    """Gateway for the USGS FDSN earthquake event catalog."""

    service_key: ClassVar[str] = "usgs_earthquakes"
    paid_service: ClassVar[bool] = False

    def get_recent_nearby_earthquakes(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_km: float = 100,
        min_magnitude: float = 2.5,
        years: int = 10,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent earthquakes near a coordinate, most recent first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_km: Search radius in kilometers.
            min_magnitude: Minimum magnitude to include.
            years: How many years back to search.
            limit: Maximum number of events to return.

        Returns:
            Normalized event dicts, most recent first; empty when nothing
            matches or the request fails.
        """
        start_time = (datetime.now(tz=UTC) - timedelta(days=365 * years)).strftime("%Y-%m-%d")
        params: dict[str, Any] = {
            "format": "geojson",
            "latitude": latitude,
            "longitude": longitude,
            "maxradiuskm": radius_km,
            "minmagnitude": min_magnitude,
            "starttime": start_time,
            "orderby": "time",
            "limit": max(1, min(int(limit), 200)),
        }
        try:
            response = self.session.get(_QUERY_URL, params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("USGS earthquake query failed for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return []
        return [_normalize_event(feature) for feature in body.get("features") or []]
