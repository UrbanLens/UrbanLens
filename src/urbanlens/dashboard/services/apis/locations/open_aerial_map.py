"""OpenAerialMap gateway for openly licensed aerial imagery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.meta import create_bbox
from urbanlens.dashboard.services.gateway import Gateway

_BASE_URL = "https://api.openaerialmap.org"


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAerialMapGateway(Gateway):
    """Gateway for OpenAerialMap imagery metadata and tile indexes."""

    service_key: ClassVar[str] = "open_aerial_map"

    def search_imagery_for_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        delta: float = 0.005,
        limit: int = 10,
        sort: str = "-acquisition_end",
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Search OpenAerialMap image metadata around coordinates."""
        params: dict[str, Any] = {"bbox": create_bbox(latitude, longitude, delta), "limit": limit, "sort": sort}
        if provider:
            params["provider"] = provider
        response = self.session.get(f"{_BASE_URL}/meta", params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def list_tile_services_for_coordinates(self, latitude: float, longitude: float, *, delta: float = 0.005, limit: int = 10) -> dict[str, Any]:
        """List available OpenAerialMap TMS services around coordinates."""
        response = self.session.get(f"{_BASE_URL}/tms", params={"bbox": create_bbox(latitude, longitude, delta), "limit": limit}, timeout=20)
        response.raise_for_status()
        return response.json()

    def imagery_statistics_for_bbox(self, west: float, south: float, east: float, north: float) -> dict[str, Any]:
        """Return OpenAerialMap analytics for a bounding box."""
        response = self.session.get(f"{_BASE_URL}/analytics", params={"bbox": f"{west},{south},{east},{north}"}, timeout=20)
        response.raise_for_status()
        return response.json()
