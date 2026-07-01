"""Apple Maps Server API gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

_BASE_URL = "https://maps-api.apple.com/v1"


@dataclass(slots=True, kw_only=True)
class AppleMapsGateway(Gateway):
    """Gateway for Apple Maps Server API endpoints.

    Authentication:
        Apple Maps Server API uses JSON Web Token (JWT) authentication.
        ``api_key`` must be a signed JWT generated from your Apple Developer
        account credentials (private key ``.p8`` file, Team ID, Maps ID, Key ID).

        JWTs can be valid for up to 6 months — generate a long-lived token for
        server-side use and store it as ``UL_APPLE_MAPS_API_KEY`` in ``.env``.

        Apple Developer docs:
        https://developer.apple.com/documentation/applemapsserverapi/creating_a_maps_identifier_and_a_private_key
    """

    service_key: ClassVar[str] = "apple_maps"

    api_key: str | None = field(default_factory=lambda: settings.apple_maps_api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError(
                "Apple Maps JWT is not set. Set UL_APPLE_MAPS_API_KEY to a signed JWT. "
                "See https://developer.apple.com/documentation/applemapsserverapi/",
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    def search_near_coordinates(
        self,
        latitude: float,
        longitude: float,
        query: str,
        *,
        radius: int | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Search for places near coordinates.

        Args:
            latitude: WGS-84 latitude of the search origin.
            longitude: WGS-84 longitude of the search origin.
            query: Free-text search query (e.g. ``"coffee shop"``).
            radius: Optional search radius in meters.
            **params: Additional Apple Maps API parameters (``lang``, ``resultTypeFilter``, etc.).

        Returns:
            Parsed JSON response with ``results`` list of place objects.
        """
        request_params: dict[str, Any] = {"q": query, "searchLocation": f"{latitude},{longitude}", **params}
        if radius is not None:
            request_params["searchRegion"] = f"{latitude},{longitude},{radius}"
        response = self.session.get(f"{_BASE_URL}/search", headers=self._headers(), params=request_params, timeout=10)
        response.raise_for_status()
        return response.json()

    def reverse_geocode_coordinates(self, latitude: float, longitude: float, **params: Any) -> dict[str, Any]:
        """Reverse geocode coordinates to addresses or places.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            **params: Additional API parameters (``lang``, ``limitToCountries``, etc.).

        Returns:
            Parsed JSON with a ``results`` list of address objects.
        """
        response = self.session.get(
            f"{_BASE_URL}/reverseGeocode",
            headers=self._headers(),
            params={"loc": f"{latitude},{longitude}", **params},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def geocode_address(self, query: str, **params: Any) -> dict[str, Any]:
        """Forward geocode a structured or free-form address.

        Args:
            query: Address string to geocode.
            **params: Additional API parameters (``lang``, ``limitToCountries``,
                ``searchLocation``, ``userLocation``, etc.).

        Returns:
            Parsed JSON with a ``results`` list of geocoded place objects.
        """
        response = self.session.get(f"{_BASE_URL}/geocode", headers=self._headers(), params={"q": query, **params}, timeout=10)
        response.raise_for_status()
        return response.json()

    def eta_from_coordinates(
        self,
        origin_latitude: float,
        origin_longitude: float,
        destination_latitude: float,
        destination_longitude: float,
        **params: Any,
    ) -> dict[str, Any]:
        """Return estimated travel time between two coordinate pairs.

        Args:
            origin_latitude: Starting point latitude.
            origin_longitude: Starting point longitude.
            destination_latitude: Destination latitude.
            destination_longitude: Destination longitude.
            **params: Additional API parameters (``transportType``, ``departureDate``, etc.).

        Returns:
            Parsed JSON with an ``etas`` list of ETA results, one per transport type.
        """
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
