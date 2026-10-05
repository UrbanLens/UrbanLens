"""Azure Maps Geocoding API: forward and reverse geocoding."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from urbanlens.dashboard.services.apis.locations.azure.gateway import AzureMapsGateway
from urbanlens.dashboard.services.core.input_validation import require_coordinates, require_query

logger = logging.getLogger(__name__)

#: Azure Maps Geocoding API version this gateway targets.
GEOCODING_API_VERSION = "2023-06-01"


def _normalize_feature(feature: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Geocoding API GeoJSON ``Feature`` into a display-friendly dict."""
    properties = feature.get("properties") or {}
    address = properties.get("address") or {}
    coordinates = (feature.get("geometry") or {}).get("coordinates") or [None, None]
    admin_districts = address.get("adminDistricts") or []

    return {
        "name": address.get("locality") or address.get("formattedAddress") or "",
        "formatted_address": address.get("formattedAddress") or "",
        "confidence": properties.get("confidence") or "",
        "match_codes": properties.get("matchCodes") or [],
        "entity_type": properties.get("type") or "",
        "street": address.get("addressLine") or "",
        "locality": address.get("locality") or "",
        "postal_code": address.get("postalCode") or "",
        "admin_district": admin_districts[0].get("name") if admin_districts else "",
        "country": (address.get("countryRegion") or {}).get("name") or "",
        "longitude": coordinates[0],
        "latitude": coordinates[1],
        "bbox": feature.get("bbox") or [],
    }


@dataclass(slots=True, kw_only=True)
class AzureMapsGeocodingGateway(AzureMapsGateway):
    """Forward and reverse geocoding via the Azure Maps Geocoding API."""

    def geocode_address(self, query: str) -> dict[str, Any] | None:
        """Forward-geocode a free-text address or place name.

        Args:
            query: Address or place-name query string.

        Returns:
            The best-matching normalized result, or None when nothing matched.

        Raises:
            ImpossibleInputError: The query is only whitespace.
            ValueError: When no subscription key is configured.
            requests.exceptions.RequestException: When the request failed.
        """
        if not query:
            return None
        require_query(self.service_key, query)
        body = self._get("/geocode", api_version=GEOCODING_API_VERSION, params={"query": query})
        features = body.get("features") or []
        return _normalize_feature(features[0]) if features else None

    def reverse_geocode(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Reverse-geocode coordinates to a formatted address.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            The normalized address result, or None when nothing matched.

        Raises:
            ImpossibleInputError: The point is not on the globe, or is ``(0, 0)``, which has no address.
            ValueError: When no subscription key is configured.
            requests.exceptions.RequestException: When the request failed.
        """
        require_coordinates(self.service_key, latitude, longitude)
        body = self._get(
            "/reverseGeocode",
            api_version=GEOCODING_API_VERSION,
            params={"coordinates": f"{longitude},{latitude}"},
        )
        features = body.get("features") or []
        return _normalize_feature(features[0]) if features else None
