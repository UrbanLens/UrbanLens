"""Boundary provider backed by REData's authoritative county parcel/building geometry."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar

from django.contrib.gis.geos import MultiPoint, MultiPolygon, Point, Polygon

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, geojson_polygon_to_geos
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


def _largest_polygon(geom: Polygon | MultiPolygon | None) -> Polygon | None:
    """Reduce a possibly-multi-shell result to the single largest polygon."""
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        candidates = [polygon for polygon in geom if isinstance(polygon, Polygon)]
        return max(candidates, key=lambda polygon: polygon.area) if candidates else None
    return None


def suggested_boundary(candidates: list[dict]) -> Polygon | MultiPolygon | None:
    """Pick the candidate REData scored highest, by REData's own rule.
    The array is not sorted, so taking the first element picks a boundary at random.

    Args:
        candidates: Records from ``RedataGateway.lookup_boundaries``.

    Returns:
        The chosen geometry, or None when nothing usable came back."""
    usable = [(c, polygon) for c in candidates if isinstance(c, dict) and (polygon := geojson_polygon_to_geos(c.get("geometry"))) is not None]
    for candidate, polygon in usable:
        if candidate.get("is_suggested"):
            return polygon

    def rank(entry: tuple[dict, Polygon | MultiPolygon]) -> tuple[int, float, float]:
        candidate, polygon = entry
        return (0 if candidate.get("kind") == "parcel" else 1, -float(candidate.get("confidence") or 0.0), polygon.area)

    return min(usable, key=rank)[1] if usable else None


@dataclass(slots=True)
class RedataBoundaryProvider(BoundaryProvider):
    """Property and building boundaries sourced from REData's county GIS data."""

    service_key: ClassVar[str | None] = "redata_boundary"
    boundary_kind: ClassVar[str] = "property"

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """Untyped convenience lookup - see :meth:`get_typed_boundaries` for the real logic."""
        return _largest_polygon(self.get_typed_boundaries(latitude, longitude, name=name).get("property"))

    def get_typed_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> dict[str, Polygon | MultiPolygon | None]:
        """Fetch REData's parcel record for this coordinate and convert its geometry fields.

        Returns:
            ``{"property": ..., "building": ...}``, both possibly None."""
        if not settings.redata_api_url or not settings.redata_api_key:
            return {}

        gateway = RedataGateway()
        try:
            payload = gateway.lookup_parcel(latitude, longitude)
        except PropertyRecordsUnavailableError as exc:
            logger.debug("REData boundary lookup unavailable for %s: %s", self.service_key, exc)
            return {"property": None, "building": None}

        property_polygon = geojson_polygon_to_geos(payload.get("parcel_geometry"))
        if property_polygon is None:
            property_polygon = self._scored_boundary(gateway, payload.get("uuid"))
        if property_polygon is None:
            property_polygon = self._buildings_convex_hull(gateway, payload.get("uuid"))

        return {
            "property": property_polygon,
            "building": geojson_polygon_to_geos(payload.get("building_geometry")),
        }

    def _scored_boundary(self, gateway: RedataGateway, parcel_uuid: str | None) -> Polygon | MultiPolygon | None:
        """REData's own best boundary for this parcel, when it has no cadastral line.
        ``parcel_geometry`` is null for every New York parcel by construction - the state's Tier 1 source is a centroid point layer, so there is no ring to extract - which is why this path, not the cadastral one, is what a NY pin actually resolves through.

        Returns:
            The suggested boundary, or None when REData offers no candidate or the request failed."""
        if not parcel_uuid:
            return None
        try:
            candidates = gateway.lookup_boundaries(parcel_uuid)
        except PropertyRecordsUnavailableError as exc:
            logger.debug("REData boundary candidates unavailable for parcel %s: %s", parcel_uuid, exc)
            return None
        return suggested_boundary(candidates)

    def _buildings_convex_hull(self, gateway: RedataGateway, parcel_uuid: str | None) -> Polygon | None:
        """Approximate the property boundary as the convex hull of the parcel's own buildings.
        A jurisdiction that never digitized a parcel-boundary shapefile can still publish individual building locations (county GIS or NY SHPO's CRIS inventory), since those only need a point each.

        Returns:
            A convex-hull ``Polygon`` around the parcel's building coordinates, or None when there's no uuid, fewer than 3 usable coordinates, the points are collinear (the hull degenerates to a line or point), or the buildings lookup itself failed."""
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property

        if not parcel_uuid:
            return None
        try:
            buildings = gateway.lookup_buildings(parcel_uuid)
        except PropertyRecordsUnavailableError as exc:
            logger.debug("REData buildings lookup unavailable for parcel %s: %s", parcel_uuid, exc)
            return None

        points: list[Point] = []
        for building in buildings_on_property(buildings):
            latitude, longitude = building.get("latitude"), building.get("longitude")
            if latitude is None or longitude is None:
                continue
            points.append(Point(float(longitude), float(latitude), srid=4326))
        if len(points) < 3:
            return None

        hull = MultiPoint(points, srid=4326).convex_hull
        return hull if isinstance(hull, Polygon) else None
