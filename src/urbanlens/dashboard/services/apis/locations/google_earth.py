"""Google Earth Engine and Google Earth Web gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlencode

from urbanlens.dashboard.services.apis.locations.base import create_bbox_str
from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

_EARTH_ENGINE_URL = "https://earthengine.googleapis.com/v1"
_DATA_CATALOG_URL = "https://developers.google.com/earth-engine/datasets/catalog"
_EARTH_WEB_URL = "https://earth.google.com/web"


@dataclass(slots=True, kw_only=True)
class GoogleEarthGateway(Gateway):
    """Gateway for Google Earth Engine REST API and Google Earth Web deep links."""

    service_key: ClassVar[str] = "google_earth"
    paid_service: ClassVar[bool] = True

    api_key: str | None = field(default_factory=lambda: settings.google_earth_api_key)

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization header with the configured OAuth2 Bearer token.

        Raises:
                ValueError: When no OAuth2 token is configured."""
        if not self.api_key:
            raise ValueError(
                "Google Earth Engine OAuth2 token is not set. Set UL_GOOGLE_EARTH_API_KEY to an OAuth2 Bearer token obtained from Application Default Credentials or a service account. See https://developers.google.com/earth-engine/guides/auth",
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    # ------------------------------------------------------------------
    # URL builders - no auth required
    # ------------------------------------------------------------------

    def earth_web_url_for_coordinates(self, latitude: float, longitude: float, *, altitude: float = 500.0) -> str:
        """Return a Google Earth Web URL that flies to the given coordinates.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            altitude: Camera altitude in metres above the surface (default 500 m,
                roughly city-block zoom level).

        Returns:
            Deep-link URL to Google Earth Web centred on the coordinates.
        """
        return f"{_EARTH_WEB_URL}/@{latitude},{longitude},{altitude}a,0d,30y"

    def catalog_url(self, search: str | None = None) -> str:
        """Return the Earth Engine public data catalog URL.

        Args:
            search: Optional free-text filter to pre-populate the catalog search.

        Returns:
            URL to the Earth Engine dataset catalog, optionally filtered.
        """
        if not search:
            return _DATA_CATALOG_URL
        return f"{_DATA_CATALOG_URL}?{urlencode({'q': search})}"

    def code_editor_url_for_coordinates(self, latitude: float, longitude: float, *, zoom: int = 16) -> str:
        """Return a Google Earth Engine Code Editor URL centred on coordinates.

        Args:
                latitude: WGS-84 latitude.
                longitude: WGS-84 longitude.
                zoom: Map zoom level (default 16, ~street-block scale).

        Returns:
                Earth Engine Code Editor URL."""
        return f"https://code.earthengine.google.com/?lon={longitude}&lat={latitude}&zoom={zoom}"

    # ------------------------------------------------------------------
    # REST API methods - require OAuth2 Bearer token
    # ------------------------------------------------------------------

    def list_assets(self, parent: str, **params: Any) -> dict[str, Any]:
        """List Earth Engine assets under a parent collection or project.

        Args:
                parent: Full asset path, e.g.
                ``"projects/earthengine-public/assets/COPERNICUS/S2"`` or
                ``"projects/my-project/assets"``.
                **params: Additional query parameters (``pageToken``, ``pageSize``, etc.).

        Returns:
                Parsed JSON with an ``assets`` list."""
        response = self.session.get(
            f"{_EARTH_ENGINE_URL}/{parent}/assets",
            headers=self._auth_headers(),
            params=params or None,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def get_asset(self, name: str) -> dict[str, Any]:
        """Return Earth Engine asset metadata.

        Args:
                name: Full asset path, e.g.
                ``"projects/earthengine-public/assets/COPERNICUS/S2/20200101T000000"``.

        Returns:
                Parsed JSON asset metadata object."""
        response = self.session.get(
            f"{_EARTH_ENGINE_URL}/{name}",
            headers=self._auth_headers(),
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def compute_value_for_coordinates(
        self,
        expression: dict[str, Any],
        latitude: float,
        longitude: float,
        *,
        delta: float = 0.005,
    ) -> dict[str, Any]:
        """Evaluate an Earth Engine expression graph near a coordinate.

        Args:
                expression: Serialised EE computation graph (``ee.Image(...).serialize()``).
                latitude: WGS-84 latitude used to build the region hint.
                longitude: WGS-84 longitude used to build the region hint.
                delta: Half-width of the bounding box added as a region hint.

        Returns:
                Parsed JSON with a ``result`` key containing the computed value."""
        response = self.session.post(
            f"{_EARTH_ENGINE_URL}/projects/earthengine-legacy/value:compute",
            headers=self._auth_headers(),
            json={"expression": expression, "bbox": create_bbox_str(latitude, longitude, delta)},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
