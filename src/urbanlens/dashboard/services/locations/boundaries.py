"""Boundary-provider abstractions for default Location geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Protocol

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point, Polygon

from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.microsoft_buildings import MicrosoftBuildingFootprintsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.regrid import RegridGateway
from urbanlens.dashboard.services.apis.locations.overpass import OverpassGateway

logger = logging.getLogger(__name__)

DEFAULT_BBOX_DEGREES = 0.00045
BOUNDARY_LOOKUP_BBOX_DEGREES = 0.001
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
class RegridBoundaryProvider:
    """Boundary provider backed by Regrid parcel/building geometry."""

    gateway: RegridGateway | None = None
    name: str = "regrid"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        gateway = self.gateway or RegridGateway()
        payload = gateway.get_parcel_by_point(
            latitude,
            longitude,
            radius=0,
            limit=5,
            return_matched_buildings=True,
        )
        return _best_containing_polygon(_features_from_payload(payload), latitude, longitude)


@dataclass(slots=True)
class OvertureMapsBoundaryProvider:
    """Boundary provider backed by Overture Maps building footprints."""

    gateway: OvertureMapsGateway | None = None
    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES
    name: str = "overture_maps"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        gateway = self.gateway or OvertureMapsGateway()
        return _best_containing_polygon(
            _features_from_geodataframe(gateway.get_buildings(_lookup_bbox(latitude, longitude, self.bbox_delta))),
            latitude,
            longitude,
        )


@dataclass(slots=True)
class MicrosoftBuildingsBoundaryProvider:
    """Boundary provider backed by Microsoft's Global ML Building Footprints."""

    gateway: MicrosoftBuildingFootprintsGateway | None = None
    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES
    name: str = "microsoft_building_footprints"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        gateway = self.gateway or MicrosoftBuildingFootprintsGateway()
        return _best_containing_polygon(
            gateway.get_buildings(_lookup_bbox(latitude, longitude, self.bbox_delta)),
            latitude,
            longitude,
        )


@dataclass(slots=True)
class GoogleOpenBuildingsBoundaryProvider:
    """Boundary provider backed by Google's Open Buildings dataset."""

    gateway: GoogleOpenBuildingsGateway | None = None
    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES
    name: str = "google_open_buildings"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        gateway = self.gateway or GoogleOpenBuildingsGateway()
        return _best_containing_polygon(
            gateway.get_buildings(_lookup_bbox(latitude, longitude, self.bbox_delta)),
            latitude,
            longitude,
        )


@dataclass(slots=True)
class StaticDefaultBoundaryProvider:
    """Deterministic fallback used when external providers cannot find a boundary."""

    name: str = "default_bbox"

    def boundary_for_point(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon:
        return default_bbox(latitude, longitude)


@dataclass(slots=True)
class BoundaryProviderChain:
    """Resolve default boundaries by trying providers in order until one succeeds."""

    providers: tuple[BoundaryProvider, ...] = field(
        default_factory=lambda: (
            OverpassBoundaryProvider(),
            RegridBoundaryProvider(),
            OvertureMapsBoundaryProvider(),
            MicrosoftBuildingsBoundaryProvider(),
            GoogleOpenBuildingsBoundaryProvider(),
            StaticDefaultBoundaryProvider(),
        ),
    )

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


def _lookup_bbox(latitude: float, longitude: float, delta: float) -> tuple[float, float, float, float]:
    return (longitude - delta, latitude - delta, longitude + delta, latitude + delta)


def _features_from_payload(payload: dict) -> list[dict]:
    features: list[dict] = []
    for key in ("buildings", "parcels"):
        collection = payload.get(key)
        if isinstance(collection, dict) and isinstance(collection.get("features"), list):
            features.extend(collection["features"])
    if isinstance(payload.get("features"), list):
        features.extend(payload["features"])
    return features


def _features_from_geodataframe(frame) -> list[dict]:
    if hasattr(frame, "iterfeatures"):
        return list(frame.iterfeatures())
    if isinstance(frame, list):
        return frame
    return []


def _best_containing_polygon(features: list[dict], latitude: float, longitude: float) -> Polygon | None:
    point = Point(float(longitude), float(latitude), srid=4326)
    candidates: list[Polygon] = []
    for feature in features:
        polygon = _polygon_from_feature(feature)
        if polygon is None or not _is_reasonable_default(polygon):
            continue
        if polygon.contains(point) or polygon.touches(point):
            candidates.append(polygon)
    if not candidates:
        return None
    return min(candidates, key=lambda polygon: polygon.area)


def _polygon_from_feature(feature: dict) -> Polygon | None:
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if geometry is None and hasattr(feature, "__geo_interface__"):
        geometry = feature.__geo_interface__
    if hasattr(geometry, "__geo_interface__"):
        geometry = geometry.__geo_interface__
    if not geometry:
        return None
    try:
        geom = GEOSGeometry(json.dumps(geometry), srid=4326)
    except (TypeError, ValueError):
        return None
    return _best_polygon_from_geometry(geom)


def _best_polygon_from_geometry(geom: GEOSGeometry) -> Polygon | None:
    if geom.empty:
        return None
    if not geom.valid:
        geom = geom.buffer(0)
    if isinstance(geom, Polygon):
        return geom if geom.valid and not geom.empty else None
    if isinstance(geom, MultiPolygon):
        polygons = [polygon for polygon in geom if polygon.valid and not polygon.empty]
        return max(polygons, key=lambda polygon: polygon.area) if polygons else None
    return None


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
    return _best_polygon_from_geometry(geom)


def _is_reasonable_default(polygon: Polygon) -> bool:
    return 0 < polygon.area <= MAX_DEFAULT_BOUNDARY_AREA_DEGREES
