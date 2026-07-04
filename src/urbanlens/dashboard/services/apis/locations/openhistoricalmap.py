"""OpenHistoricalMap gateway for historic OSM-style data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import create_bbox_str
from urbanlens.dashboard.services.gateway import Gateway

_API_URL = "https://www.openhistoricalmap.org/api/0.6"
_NOMINATIM_URL = "https://nominatim.openhistoricalmap.org"
_OVERPASS_URL = "https://overpass-api.openhistoricalmap.org/api/interpreter"
_TILE_URL = "https://tile.openhistoricalmap.org"


@dataclass(slots=True, kw_only=True)
class OpenHistoricalMapGateway(Gateway):
    """Gateway for OpenHistoricalMap API, search, Overpass, and tile services."""

    service_key: ClassVar[str] = "openhistoricalmap"
    paid_service: ClassVar[bool] = False

    def search(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Search OpenHistoricalMap by name or address."""
        response = self.session.get(f"{_NOMINATIM_URL}/search", params={"q": query, "format": "jsonv2", **params}, timeout=10)
        response.raise_for_status()
        return response.json()

    def search_near_coordinates(self, latitude: float, longitude: float, query: str, *, delta: float = 0.005, **params: Any) -> list[dict[str, Any]]:
        """Search OpenHistoricalMap for named historic features near coordinates."""
        return self.search(query, viewbox=create_bbox_str(latitude, longitude, delta), bounded=1, **params)

    def reverse_geocode_coordinates(self, latitude: float, longitude: float, **params: Any) -> dict[str, Any]:
        """Reverse geocode coordinates against OpenHistoricalMap."""
        response = self.session.get(f"{_NOMINATIM_URL}/reverse", params={"lat": latitude, "lon": longitude, "format": "jsonv2", **params}, timeout=10)
        response.raise_for_status()
        return response.json()

    def overpass_query(self, query: str) -> dict[str, Any]:
        """Run a raw OpenHistoricalMap Overpass query."""
        response = self.session.post(_OVERPASS_URL, data={"data": query}, timeout=30)
        response.raise_for_status()
        return response.json()

    def overpass_features_near_coordinates(self, latitude: float, longitude: float, *, radius_meters: int = 100, tag_filter: str = "[historic]") -> dict[str, Any]:
        """Return historic Overpass features around coordinates."""
        query = f"""
[out:json][timeout:25];
(
  node(around:{radius_meters},{latitude},{longitude}){tag_filter};
  way(around:{radius_meters},{latitude},{longitude}){tag_filter};
  relation(around:{radius_meters},{latitude},{longitude}){tag_filter};
);
out center tags;
""".strip()
        return self.overpass_query(query)

    def map_xml_for_coordinates(self, latitude: float, longitude: float, *, delta: float = 0.005) -> str:
        """Return raw OSM XML for map data around coordinates."""
        response = self.session.get(f"{_API_URL}/map", params={"bbox": create_bbox_str(latitude, longitude, delta)}, timeout=20)
        response.raise_for_status()
        return response.text

    def tile_url(self, z: int, x: int, y: int) -> str:
        """Return an OpenHistoricalMap raster tile URL."""
        return f"{_TILE_URL}/{z}/{x}/{y}.png"
