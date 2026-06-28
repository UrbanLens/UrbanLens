"""OpenStreetMap Nominatim service — free reverse geocoding with place metadata."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_API_URL = "https://nominatim.openstreetmap.org"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"


@dataclass(frozen=True, slots=True, kw_only=True)
class NominatimGateway(Gateway):
    """
    Reverse-geocodes coordinates via the Nominatim API and returns rich place metadata.

    Nominatim is free and requires no API key, but enforces a 1-request/second
    rate limit.  The LocationCache layer ensures we only query once per 7 days,
    so this is not a concern in practice.
    """

    base_url: str = _API_URL

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def reverse_geocode(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """
        Reverse-geocode coordinates and return structured place metadata.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Dict with place metadata, or None if no result or an error occurred.
        """
        try:
            resp = self.session.get(
                f"{self.base_url}/reverse",
                params={
                    "lat": latitude,
                    "lon": longitude,
                    "format": "json",
                    "extratags": 1,
                    "namedetails": 1,
                    "addressdetails": 1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            logger.exception("Nominatim reverse geocode failed for %s,%s", latitude, longitude)
            return None

        if "error" in raw:
            return None

        return self._normalise(raw)

    @staticmethod
    def _normalise(raw: dict) -> dict[str, Any]:
        """Extract useful fields from the raw Nominatim response."""
        extra = raw.get("extratags") or {}
        address = raw.get("address") or {}
        osm_type = raw.get("osm_type", "")
        osm_id = raw.get("osm_id", "")

        osm_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_type and osm_id else ""

        name = (
            (raw.get("namedetails") or {}).get("name")
            or raw.get("name")
            or address.get("amenity")
            or address.get("building")
            or ""
        )

        return {
            "name": name,
            "display_name": raw.get("display_name", ""),
            "osm_url": osm_url,
            "website": extra.get("website") or extra.get("url") or "",
            "phone": extra.get("phone") or extra.get("contact:phone") or "",
            "opening_hours": extra.get("opening_hours") or "",
            "operator": extra.get("operator") or "",
            "building": extra.get("building") or address.get("building") or "",
            "amenity": address.get("amenity") or "",
            "tourism": extra.get("tourism") or address.get("tourism") or "",
            "historic": extra.get("historic") or address.get("historic") or "",
            "wikipedia": extra.get("wikipedia") or "",
        }
