"""Photon geocoder gateway - Komoot's free, keyless, open-source OSM geocoder.

https://github.com/komoot/photon - an alternate geocoder to the existing
Nominatim integration, for redundancy and cross-checking. Both read the same
underlying OpenStreetMap data through different indexing/ranking software.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_API_URL = "https://photon.komoot.io"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"


def _normalize_feature(feature: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Photon GeoJSON ``Feature`` into a display-friendly dict."""
    properties = feature.get("properties") or {}
    coordinates = (feature.get("geometry") or {}).get("coordinates") or [None, None]
    return {
        "name": properties.get("name") or "",
        "osm_key": properties.get("osm_key") or "",
        "osm_value": properties.get("osm_value") or "",
        "housenumber": properties.get("housenumber") or "",
        "street": properties.get("street") or "",
        "locality": properties.get("locality") or "",
        "district": properties.get("district") or "",
        "city": properties.get("city") or "",
        "county": properties.get("county") or "",
        "state": properties.get("state") or "",
        "country": properties.get("country") or "",
        "postcode": properties.get("postcode") or "",
        "longitude": coordinates[0],
        "latitude": coordinates[1],
    }


@dataclass(slots=True, kw_only=True)
class PhotonGateway(Gateway):
    """Gateway for the Photon geocoder's free public instance (photon.komoot.io).

    No API key required. Be conservative with request volume - it's a shared
    community resource, not a dedicated account.
    """

    service_key: ClassVar[str] = "photon"
    paid_service: ClassVar[bool] = False

    base_url: str = _API_URL

    def __post_init__(self) -> None:
        """Attach a descriptive User-Agent - the public instance 403s the default one."""
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def search(self, query: str, *, limit: int = 5, **params: Any) -> list[dict[str, Any]]:
        """Free-text forward search across addresses and named places.

        Args:
            query: Free-text search query.
            limit: Maximum number of results (1-50).
            **params: Additional Photon query parameters (e.g. ``lat``/``lon``
                to bias results toward a location).

        Returns:
            Normalized result dicts, most relevant first; empty on failure.
        """
        if not query:
            return []
        request_params: dict[str, Any] = {"q": query, "limit": max(1, min(int(limit), 50)), **params}
        try:
            response = self.session.get(f"{self.base_url}/api", params=request_params, timeout=10)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("Photon search failed for %r", query, exc_info=True)
            return []
        return [_normalize_feature(feature) for feature in body.get("features") or []]

    def reverse_geocode(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Reverse-geocode coordinates to the nearest OSM feature.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            The normalized nearest-feature dict, or None when nothing
            resolved or the request failed.
        """
        try:
            response = self.session.get(f"{self.base_url}/reverse", params={"lat": latitude, "lon": longitude}, timeout=10)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("Photon reverse geocode failed for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return None
        features = body.get("features") or []
        return _normalize_feature(features[0]) if features else None
