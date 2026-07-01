"""Apple Maps Server API gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

_BASE_URL = "https://maps-api.apple.com/v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class AppleMapsGateway(Gateway):
    """Gateway for Apple Maps Server API endpoints."""

    service_key: ClassVar[str] = "apple_maps"

    api_key: str | None = field(default_factory=lambda: settings.apple_maps_api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("Apple Maps API key is not set")
        return {"Authorization": f"Bearer {self.api_key}"}

    def search_near_coordinates(self, latitude: float, longitude: float, query: str, *, radius: int | None = None, **params: Any) -> dict[str, Any]:
        """Search for places near coordinates."""
        request_params: dict[str, Any] = {"q": query, "searchLocation": f"{latitude},{longitude}", **params}
        if radius is not None:
            request_params["searchRegion"] = f"{latitude},{longitude},{radius}"
        response = self.session.get(f"{_BASE_URL}/search", headers=self._headers(), params=request_params, timeout=10)
        response.raise_for_status()
        return response.json()

    def reverse_geocode_coordinates(self, latitude: float, longitude: float, **params: Any) -> dict[str, Any]:
        """Reverse geocode coordinates to addresses or places."""
        response = self.session.get(
            f"{_BASE_URL}/reverseGeocode",
            headers=self._headers(),
            params={"loc": f"{latitude},{longitude}", **params},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def geocode_address(self, query: str, **params: Any) -> dict[str, Any]:
        """Forward geocode a structured or free-form address."""
        response = self.session.get(f"{_BASE_URL}/geocode", headers=self._headers(), params={"q": query, **params}, timeout=10)
        response.raise_for_status()
        return response.json()

    def eta_from_coordinates(self, origin_latitude: float, origin_longitude: float, destination_latitude: float, destination_longitude: float, **params: Any) -> dict[str, Any]:
        """Return estimated travel time between two coordinate pairs."""
        response = self.session.get(
            f"{_BASE_URL}/etas",
            headers=self._headers(),
            params={
                "origin": f"{origin_latitude},{origin_longitude}",
                "destination": f"{destination_latitude},{destination_longitude}",
                **params,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
