"""USGS gateway for M2M/EarthExplorer and The National Map services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.meta import create_bbox
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

_M2M_URL = "https://m2m.cr.usgs.gov/api/api/json/stable"
_TNM_URL = "https://tnmaccess.nationalmap.gov/api/v1"
_HTMC_PRODUCTS = "Historical Topographic Map Collection (HTMC)"


@dataclass(frozen=True, slots=True, kw_only=True)
class UsgsGateway(Gateway):
    """Gateway for USGS M2M/EarthExplorer, TNMAccess, topoView, and HTMC."""

    service_key: ClassVar[str] = "usgs"

    api_key: str | None = field(default_factory=lambda: settings.usgs_api_key)

    def m2m_request(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"X-Auth-Token": self.api_key} if self.api_key else None
        response = self.session.post(f"{_M2M_URL}/{endpoint}", json=payload or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def m2m_login_token(self, username: str, token: str) -> dict[str, Any]:
        """Exchange a USGS username and app token for an M2M API key."""
        return self.m2m_request("login-token", {"username": username, "token": token})

    def dataset_search(self, **payload: Any) -> dict[str, Any]:
        """Search EarthExplorer/M2M datasets."""
        return self.m2m_request("dataset-search", payload)

    def search_scenes_near_coordinates(self, latitude: float, longitude: float, *, dataset_name: str, max_results: int = 25, **payload: Any) -> dict[str, Any]:
        """Search EarthExplorer scenes intersecting a coordinate."""
        spatial_filter = {
            "filterType": "mbr",
            "lowerLeft": {"latitude": latitude, "longitude": longitude},
            "upperRight": {"latitude": latitude, "longitude": longitude},
        }
        return self.m2m_request(
            "scene-search",
            {"datasetName": dataset_name, "maxResults": max_results, "spatialFilter": spatial_filter, **payload},
        )

    def download_options(self, **payload: Any) -> dict[str, Any]:
        """Return available M2M download products for scenes."""
        return self.m2m_request("download-options", payload)

    def download_request(self, **payload: Any) -> dict[str, Any]:
        """Request downloads for selected M2M products."""
        return self.m2m_request("download-request", payload)

    def tnm_products_for_coordinates(self, latitude: float, longitude: float, *, delta: float = 0.005, **params: Any) -> dict[str, Any]:
        """Return The National Map products intersecting coordinates."""
        response = self.session.get(f"{_TNM_URL}/products", params={"bbox": create_bbox(latitude, longitude, delta), **params}, timeout=20)
        response.raise_for_status()
        return response.json()

    def historical_topo_maps_for_coordinates(self, latitude: float, longitude: float, *, delta: float = 0.005, **params: Any) -> dict[str, Any]:
        """Return topoView/HTMC historical topographic maps near coordinates."""
        return self.tnm_products_for_coordinates(latitude, longitude, delta=delta, datasets=_HTMC_PRODUCTS, **params)
