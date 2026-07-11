"""Overpass API client for deriving OpenStreetMap feature data."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any, ClassVar, Literal, Protocol

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point, Polygon
import requests

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, _is_reasonable_default, best_polygon_from_geometry
from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_API_URL = "https://overpass-api.de/api/interpreter"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"
# Overpass QL has no OR operator to chain bracket filters within one statement, so
# each top-level `|`-separated clause here becomes its own unioned statement per
# element type (see `_TAG_FILTER_CLAUSE_SPLIT` / `_nearby_features_query`). The
# split only breaks on a `|` between a `]` and a `[`, so the `|` inside the regex
# alternation below is left intact.
_DEFAULT_FEATURE_TAG_FILTER = '[~"^(building|amenity|tourism|historic|leisure|landuse|industrial|man_made|shop|office)$"~"."]|["railway"="station"]'
_TAG_FILTER_CLAUSE_SPLIT = re.compile(r"(?<=\])\|(?=\[)")
OsmElementType = Literal["node", "way", "relation"]


def _polygon_from_ring(coords: list[tuple[float, float]]) -> Polygon | None:
    if len(coords) < 4:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    try:
        geom = GEOSGeometry(json.dumps({"type": "Polygon", "coordinates": [coords]}), srid=4326)
    except (TypeError, ValueError):
        return None
    if geom.empty or not isinstance(geom, Polygon):
        return None
    if not geom.valid:
        geom = geom.buffer(0)
    return best_polygon_from_geometry(geom)


def _coords_from_geometry(geometry: list) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for node in geometry:
        try:
            coords.append((float(node["lon"]), float(node["lat"])))
        except (KeyError, TypeError, ValueError):
            return []
    return coords


def _outer_rings_from_element(element: dict) -> list[list[tuple[float, float]]]:
    if isinstance(element.get("geometry"), list):
        return [_coords_from_geometry(element["geometry"])]

    members = element.get("members")
    if not isinstance(members, list):
        return []

    rings: list[list[tuple[float, float]]] = []
    for member in members:
        if member.get("role") not in {"outer", ""} or not isinstance(member.get("geometry"), list):
            continue
        coords = _coords_from_geometry(member["geometry"])
        if coords:
            rings.append(coords)
    return rings


def _polygon_from_element(element: dict) -> Polygon | None:
    """Extract the best polygon from an Overpass way or multipolygon relation."""
    rings = _outer_rings_from_element(element)
    raw_polygons = [_polygon_from_ring(ring) for ring in rings]
    polygons: list[Polygon] = [polygon for polygon in raw_polygons if polygon is not None]
    if not polygons:
        return None
    return max(polygons, key=lambda polygon: polygon.area)


@dataclass(slots=True, kw_only=True)
class OverpassGateway(Gateway, BoundaryProvider):
    """Fetch OpenStreetMap elements from Overpass."""

    service_key: ClassVar[str] = "overpass"
    paid_service: ClassVar[bool] = False

    base_url: str = _API_URL
    timeout: int = 12
    radius_meters: int = 100

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
        """Build an Overpass QL query constrained to useful place tags.

        ``tag_filter`` may contain multiple ``|``-separated Overpass filter clauses;
        each becomes its own unioned statement per element type, since Overpass QL
        has no OR operator for chaining bracket filters within a single statement.
        """
        radius = max(10, min(int(radius_meters), 250))
        lat = float(latitude)
        lon = float(longitude)
        clauses = _TAG_FILTER_CLAUSE_SPLIT.split(tag_filter)
        selectors = []
        for clause in clauses:
            if include_nodes:
                selectors.append(f"  node(around:{radius},{lat:.7f},{lon:.7f}){clause};")
            selectors.extend(
                [
                    f"  way(around:{radius},{lat:.7f},{lon:.7f}){clause};",
                    f'  relation(around:{radius},{lat:.7f},{lon:.7f})["type"="multipolygon"]{clause};',
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

    @staticmethod
    def _is_building_element(element: dict) -> bool:
        """True when an OSM element's tags describe a building footprint.

        Anything else (landuse, amenity perimeter, leisure grounds, industrial
        sites...) is treated as a property boundary - ambiguity resolves to
        property.
        """
        tags = element.get("tags")
        if not isinstance(tags, dict):
            return False
        return bool(tags.get("building") or tags.get("building:part"))

    def _containing_polygons_by_kind(self, latitude: float, longitude: float) -> dict[str, list[Polygon]]:
        """Collect polygons containing the point, split into building/property kinds."""
        point = Point(float(longitude), float(latitude), srid=4326)
        candidates: dict[str, list[Polygon]] = {"building": [], "property": []}
        for element in self.nearby_boundary_candidates(latitude, longitude, self.radius_meters):
            polygon = _polygon_from_element(element)
            if polygon is None or not _is_reasonable_default(polygon):
                continue
            if polygon.contains(point) or polygon.touches(point):
                kind = "building" if self._is_building_element(element) else "property"
                candidates[kind].append(polygon)
        return candidates

    def get_typed_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> dict[str, Polygon | None]:
        """Return the smallest containing building footprint and property perimeter.

        One Overpass query yields both kinds: elements tagged ``building`` (or
        ``building:part``) become the building boundary; every other matching
        feature (landuse, amenity, leisure, industrial...) competes for the
        property boundary.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Unused; Overpass matches spatially.

        Returns:
            Mapping with "building" and "property" keys (values may be None).
        """
        candidates = self._containing_polygons_by_kind(latitude, longitude)
        return {kind: (min(polygons, key=lambda polygon: polygon.area) if polygons else None) for kind, polygons in candidates.items()}

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """Smallest containing polygon of any kind (property preferred, else building)."""
        typed = self.get_typed_boundaries(latitude, longitude, name=name)
        return typed.get("property") or typed.get("building")
