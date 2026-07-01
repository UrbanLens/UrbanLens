"""Overpass API client for deriving OpenStreetMap feature data."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar, Literal

import requests

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_API_URL = "https://overpass-api.de/api/interpreter"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"
_DEFAULT_FEATURE_TAG_FILTER = (
    '["building"]|["amenity"]|["tourism"]|["historic"]|["leisure"]|'
    '["landuse"]|["industrial"]|["man_made"]|["shop"]|["office"]|["railway"="station"]'
)
OsmElementType = Literal["node", "way", "relation"]


@dataclass(slots=True, kw_only=True)
class OverpassGateway(Gateway):
    """Fetch OpenStreetMap elements from Overpass."""

    service_key: ClassVar[str] = "overpass"
    paid_service: ClassVar[bool] = False

    base_url: str = _API_URL
    timeout: int = 12

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def query(self, query: str, *, timeout: int | None = None) -> dict[str, Any]:
        """Run a raw Overpass QL query and return the decoded JSON payload."""
        response = self.session.post(self.base_url, data={"data": query}, timeout=timeout or self.timeout)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def elements_for_query(self, query: str, *, timeout: int | None = None) -> list[dict[str, Any]]:
        """Run Overpass QL and return the element list, logging failures as empty results."""
        try:
            payload = self.query(query, timeout=timeout)
        except (requests.RequestException, ValueError):
            logger.exception("Overpass query failed")
            return []
        elements = payload.get("elements")
        return elements if isinstance(elements, list) else []

    def nearby_features(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_meters: int = 100,
        tag_filter: str = _DEFAULT_FEATURE_TAG_FILTER,
        include_nodes: bool = True,
        include_geometry: bool = False,
    ) -> list[dict[str, Any]]:
        """Return OSM features around a coordinate for generic location enrichment."""
        query = self._nearby_features_query(
            latitude,
            longitude,
            radius_meters=radius_meters,
            tag_filter=tag_filter,
            include_nodes=include_nodes,
            include_geometry=include_geometry,
        )
        return self.elements_for_query(query)

    def nearby_boundary_candidates(self, latitude: float, longitude: float, radius_meters: int = 100) -> list[dict[str, Any]]:
        """Return OSM ways/relations likely to describe a real place boundary near a coordinate."""
        return self.nearby_features(latitude, longitude, radius_meters=radius_meters, include_nodes=False, include_geometry=True)

    def element(self, element_type: OsmElementType, osm_id: int, *, include_geometry: bool = True) -> dict[str, Any] | None:
        """Return a single OSM node, way, or relation by id via Overpass."""
        out_clause = "out tags geom;" if include_geometry else "out tags center;"
        query = f"""
[out:json][timeout:8];
{element_type}({int(osm_id)});
{out_clause}
""".strip()
        elements = self.elements_for_query(query)
        return elements[0] if elements else None

    @staticmethod
    def _nearby_features_query(
        latitude: float,
        longitude: float,
        *,
        radius_meters: int,
        tag_filter: str,
        include_nodes: bool,
        include_geometry: bool,
    ) -> str:
        """Build an Overpass QL query constrained to useful place tags."""
        radius = max(10, min(int(radius_meters), 250))
        lat = float(latitude)
        lon = float(longitude)
        selectors = []
        if include_nodes:
            selectors.append(f"  node(around:{radius},{lat:.7f},{lon:.7f}){tag_filter};")
        selectors.extend(
            [
                f"  way(around:{radius},{lat:.7f},{lon:.7f}){tag_filter};",
                f'  relation(around:{radius},{lat:.7f},{lon:.7f})["type"="multipolygon"]{tag_filter};',
            ],
        )
        out_clause = "out tags geom qt;" if include_geometry else "out center tags qt;"
        return f"""
[out:json][timeout:8];
(
{chr(10).join(selectors)}
);
{out_clause}
""".strip()
