"""Google Earth / Earth Engine gateway for historical geospatial imagery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlencode

from urbanlens.dashboard.services.apis.locations.meta import create_bbox
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

_EARTH_ENGINE_URL = "https://earthengine.googleapis.com/v1"
_DATA_CATALOG_URL = "https://developers.google.com/earth-engine/datasets/catalog"
_CODE_EDITOR_URL = "https://code.earthengine.google.com"


@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleEarthGateway(Gateway):
    """Gateway for Google Earth Engine catalog and REST endpoints.

    Google Earth desktop's historical imagery slider does not expose a public web
    API. Earth Engine is Google's supported API surface for historical imagery
    datasets.
    """

    service_key: ClassVar[str] = "google_earth"

    api_key: str | None = field(default_factory=lambda: settings.google_earth_api_key)

    def _params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = dict(params or {})
        if self.api_key:
            request_params["key"] = self.api_key
        return request_params

    def catalog_url(self, search: str | None = None) -> str:
        """Return the Earth Engine data catalog URL, optionally filtered by search text."""
        if not search:
            return _DATA_CATALOG_URL
        return f"{_DATA_CATALOG_URL}?{urlencode({'q': search})}"

    def code_editor_url_for_coordinates(self, latitude: float, longitude: float, *, zoom: int = 16) -> str:
        """Return a Google Earth Engine Code Editor URL centered on coordinates."""
        return f"{_CODE_EDITOR_URL}/?lon={longitude}&lat={latitude}&zoom={zoom}"

    def dataset_catalog_url_for_coordinates(self, latitude: float, longitude: float, *, query: str = "historical imagery") -> str:
        """Return a catalog search URL suitable for finding historical datasets for coordinates."""
        return self.catalog_url(f"{query} {latitude:.5f} {longitude:.5f}")

    def list_assets(self, parent: str, **params: Any) -> dict[str, Any]:
        """List Earth Engine assets under a parent collection or project."""
        response = self.session.get(f"{_EARTH_ENGINE_URL}/{parent}/assets", params=self._params(params), timeout=20)
        response.raise_for_status()
        return response.json()

    def get_asset(self, name: str) -> dict[str, Any]:
        """Return Earth Engine asset metadata."""
        response = self.session.get(f"{_EARTH_ENGINE_URL}/{name}", params=self._params(), timeout=20)
        response.raise_for_status()
        return response.json()

    def compute_value_for_coordinates(self, expression: dict[str, Any], latitude: float, longitude: float, *, delta: float = 0.005) -> dict[str, Any]:
        """Compute an Earth Engine expression with a coordinate bbox hint in the payload."""
        response = self.session.post(
            f"{_EARTH_ENGINE_URL}/projects/earthengine-legacy/value:compute",
            params=self._params(),
            json={"expression": expression, "bbox": create_bbox(latitude, longitude, delta)},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
