"""Shared request plumbing for the Azure Maps REST API."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)

#: Base URL for every Azure Maps REST endpoint.
AZURE_MAPS_BASE_URL = "https://atlas.microsoft.com"


def azure_maps_request(
    session: requests.Session,
    path: str,
    *,
    subscription_key: str | None,
    api_version: str,
    params: dict[str, Any] | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Issue an authenticated GET against one Azure Maps REST endpoint.

    Args:
        session: The calling gateway's (rate-limited) HTTP session.
        path: Endpoint path, e.g. ``"/geocode"``.
        subscription_key: The Azure Maps subscription key.
        api_version: The endpoint's ``api-version`` value - Azure Maps versions each product area (Search, Geocoding, Render) independently, so callers must pass the right one for the path.
        params: Additional query parameters.
        timeout: Request timeout in seconds.

    Returns:
        The parsed JSON response body.

    Raises:
        ValueError: When no subscription key is configured.
        requests.exceptions.RequestException: When the request fails."""
    if not subscription_key:
        raise ValueError("Azure Maps subscription key is not set. Set UL_AZURE_MAPS_SUBSCRIPTION_KEY in .env.")
    request_params: dict[str, Any] = {"subscription-key": subscription_key, "api-version": api_version, **(params or {})}
    response = session.get(f"{AZURE_MAPS_BASE_URL}{path}", params=request_params, timeout=timeout)
    response.raise_for_status()
    return response.json()


@dataclass(slots=True, kw_only=True)
class AzureMapsGateway(Gateway):
    """Base gateway for the Azure Maps Search and Geocoding REST APIs."""

    service_key: ClassVar[str] = "azure_maps"
    paid_service: ClassVar[bool] = True

    subscription_key: str | None = field(default_factory=lambda: settings.azure_maps_subscription_key)

    def _get(self, path: str, *, api_version: str, params: dict[str, Any] | None = None, timeout: int = 10) -> dict[str, Any]:
        """Issue an authenticated GET against one Azure Maps endpoint."""
        return azure_maps_request(self.session, path, subscription_key=self.subscription_key, api_version=api_version, params=params, timeout=timeout)
