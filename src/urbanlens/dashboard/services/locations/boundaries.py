"""Boundary-provider abstractions for default Location geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Protocol

from django.contrib.gis.geos import GEOSGeometry, Point, Polygon

from urbanlens.dashboard.services.apis.locations.overpass import OverpassGateway

logger = logging.getLogger(__name__)

DEFAULT_BBOX_DEGREES = 0.00045
MAX_DEFAULT_BOUNDARY_AREA_DEGREES = 0.02


def default_bbox(latitude: float, longitude: float) -> Polygon:
    """Return the local no-network fallback boundary around a coordinate."""
    delta = DEFAULT_BBOX_DEGREES
    return Polygon.from_bbox((longitude - delta, latitude - delta, longitude + delta, latitude + delta))


class BoundaryProvider(Protocol):
    """Provider interface for future default-boundary data sources."""

    name: str

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """Return a polygon boundary for the coordinate, or None to allow fallback."""


@dataclass(slots=True)
class OverpassBoundaryProvider:
    """Default boundary provider backed by OpenStreetMap data via Overpass."""

    gateway: OverpassGateway = field(default_factory=OverpassGateway)
    radius_meters: int = 100
    name: str = "overpass"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        point = Point(float(longitude), float(latitude), srid=4326)
        candidates: list[Polygon] = []
        for element in self.gateway.nearby_boundary_candidates(latitude, longitude, self.radius_meters):
            polygon = _polygon_from_element(element)
            if polygon is None or not _is_reasonable_default(polygon):
                continue
            if polygon.contains(point) or polygon.touches(point):
                candidates.append(polygon)
        if not candidates:
            return None
        return min(candidates, key=lambda polygon: polygon.area)


@dataclass(slots=True)
class StaticDefaultBoundaryProvider:
    """Deterministic fallback used when external providers cannot find a boundary."""

    name: str = "default_bbox"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon:
        return default_bbox(latitude, longitude)


@dataclass(slots=True)
class BoundaryProviderChain:
    """Resolve default boundaries by trying providers in order until one succeeds."""

    providers: tuple[BoundaryProvider, ...] = field(default_factory=lambda: (OverpassBoundaryProvider(), StaticDefaultBoundaryProvider()))

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon:
        for provider in self.providers:
            try:
                boundary = provider.boundary_for_point(latitude, longitude, name=name)
            except Exception:
                logger.exception("Boundary provider %s failed for %s,%s", provider.name, latitude, longitude)
                continue
            if boundary is not None:
                return boundary
        return default_bbox(latitude, longitude)


def _polygon_from_element(element: dict) -> Polygon | None:
    """Extract the best polygon from an Overpass way or multipolygon relation."""
    rings = _outer_rings_from_element(element)
    polygons = [_polygon_from_ring(ring) for ring in rings]
    polygons = [polygon for polygon in polygons if polygon is not None]
    if not polygons:
        return None
    return max(polygons, key=lambda polygon: polygon.area)


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


def _coords_from_geometry(geometry: list) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for node in geometry:
        try:
            coords.append((float(node["lon"]), float(node["lat"])))
        except (KeyError, TypeError, ValueError):
            return []
    return coords


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
    return geom if isinstance(geom, Polygon) and geom.valid and not geom.empty else None


def _is_reasonable_default(polygon: Polygon) -> bool:
    return 0 < polygon.area <= MAX_DEFAULT_BOUNDARY_AREA_DEGREES
