"""USGS gateway for M2M/EarthExplorer and The National Map services."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

from django.core.cache import cache

from urbanlens.dashboard.services.apis.locations.base import create_bbox
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_M2M_URL = "https://m2m.cr.usgs.gov/api/api/json/stable"
_TNM_URL = "https://tnmaccess.nationalmap.gov/api/v1"
_HTMC_PRODUCTS = "Historical Topographic Map Collection (HTMC)"
_M2M_SESSION_CACHE_KEY = "usgs_m2m_auth"
_M2M_SESSION_TTL = 7200  # USGS M2M session tokens expire after ~2 hours


@dataclass(slots=True, kw_only=True)
class UsgsGateway(Gateway):
    """Gateway for USGS M2M/EarthExplorer, TNMAccess, topoView, and HTMC.

    Authentication:
        USGS Machine-to-Machine (M2M) uses a two-stage authentication model:

        1. **Application token** (``UL_USGS_API_KEY``): A static token generated
           once in your USGS EarthExplorer account settings.  This never expires.

        2. **Session token**: Obtained by calling ``login-token`` with the
           application token + username.  Valid for ~2 hours and cached in
           Django's cache backend automatically.

        Set both ``UL_USGS_API_KEY`` (application token) and ``UL_USGS_USERNAME``
        in ``.env``.  The gateway handles the login exchange and token renewal
        transparently.

        TNM/topoView endpoints are public and do not require authentication.
    """

    service_key: ClassVar[str] = "usgs"
    paid_service: ClassVar[bool] = False

    api_key: str | None = field(default_factory=lambda: settings.usgs_api_key)
    username: str | None = field(default_factory=lambda: settings.usgs_username)

    def _session_token(self) -> str | None:
        """Return a valid M2M session token, exchanging credentials if needed.

        Returns:
            Session token string, or ``None`` when credentials are not configured.
        """
        token = cache.get(_M2M_SESSION_CACHE_KEY)
        if token:
            return token

        if not self.api_key or not self.username:
            return None

        try:
            resp = self.session.post(
                f"{_M2M_URL}/login-token",
                json={"username": self.username, "token": self.api_key},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("data")
            if token:
                cache.set(_M2M_SESSION_CACHE_KEY, token, _M2M_SESSION_TTL)
            return token
        except Exception:
            # TODO: Catch specific exception
            logger.exception("USGS M2M login-token exchange failed")
            return None

    def m2m_request(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a POST request to the M2M API.

        Automatically obtains and caches a session token from the configured
        application token + username credentials.

        Args:
            endpoint: M2M endpoint name (e.g. ``"scene-search"``).
            payload: Request body to serialise as JSON.

        Returns:
            Parsed JSON response.

        Raises:
            ValueError: When no credentials are configured and authentication is needed.
        """
        session_token = self._session_token()
        headers = {"X-Auth-Token": session_token} if session_token else None
        response = self.session.post(f"{_M2M_URL}/{endpoint}", json=payload or {}, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def dataset_search(self, **payload: Any) -> dict[str, Any]:
        """Search EarthExplorer/M2M datasets.

        Args:
            **payload: M2M dataset-search parameters (``datasetName``,
                ``spatialFilter``, ``temporalFilter``, etc.).

        Returns:
            Parsed JSON with a ``data`` list of matching dataset objects.
        """
        return self.m2m_request("dataset-search", payload)

    def search_scenes_near_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        dataset_name: str,
        max_results: int = 25,
        **payload: Any,
    ) -> dict[str, Any]:
        """Search EarthExplorer scenes intersecting a coordinate.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.
            dataset_name: M2M dataset identifier (e.g. ``"landsat_ot_c2_l2"``).
            max_results: Maximum scenes to return.
            **payload: Additional M2M scene-search parameters.

        Returns:
            Parsed JSON with a ``data`` object containing the matching scenes.
        """
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
        """Return available M2M download products for a set of scenes.

        Args:
            **payload: M2M download-options parameters (``datasetName``, ``entityIds``, etc.).

        Returns:
            Parsed JSON with a ``data`` list of available download products.
        """
        return self.m2m_request("download-options", payload)

    def download_request(self, **payload: Any) -> dict[str, Any]:
        """Request downloads for selected M2M products.

        Args:
            **payload: M2M download-request parameters (``downloads`` list, ``label``, etc.).

        Returns:
            Parsed JSON with download URLs and status information.
        """
        return self.m2m_request("download-request", payload)

    def tnm_products_for_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        delta: float = 0.005,
        **params: Any,
    ) -> dict[str, Any]:
        """Return The National Map products intersecting coordinates.

        No authentication required — TNM is a public endpoint.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            delta: Half-width of the bounding box in degrees.
            **params: Additional TNM API parameters (``datasets``, ``prodFormats``,
                ``prodExtents``, ``outputFormat``, etc.).

        Returns:
            Parsed JSON with a list of matching TNM products.
        """
        response = self.session.get(
            f"{_TNM_URL}/products",
            params={"bbox": create_bbox(latitude, longitude, delta), **params},
            timeout=(5, 15),
        )
        response.raise_for_status()
        return response.json()

    def historical_topo_maps_for_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        delta: float = 0.005,
        **params: Any,
    ) -> dict[str, Any]:
        """Return HTMC historical topographic maps near coordinates.

        Queries the TNM ``products`` endpoint filtered to the Historical
        Topographic Map Collection, which contains scanned USGS topo maps
        going back to the late 1800s.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            delta: Half-width of the bounding box in degrees.
            **params: Additional TNM API parameters.

        Returns:
            Parsed JSON with a list of matching HTMC products including
            download URLs for the scanned map PDFs.
        """
        return self.tnm_products_for_coordinates(latitude, longitude, delta=delta, datasets=_HTMC_PRODUCTS, **params)
