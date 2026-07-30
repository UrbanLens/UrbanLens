"""Boundary provider backed by REData's authoritative county parcel/building geometry.

Unlike every other provider in the chain (community-tagged OSM ways,
ML-derived building-footprint datasets), this is survey-grade geometry
straight from the county assessor's own GIS layer - the same data
``plugins.builtin.property_records`` already fetches for the Ownership card,
just consumed for its ``parcel_geometry``/``building_geometry`` instead of its
attribute fields.

REData's API returns these fields as standard GeoJSON, so this provider parses
that directly (``geojson_polygon_to_geos``) rather than running the
Esri-ring-fixing conversion (``esri_rings_to_polygon``), which is still needed
only for providers that hand back genuine Esri rings natively (Census
TIGERweb, via ``geo_boundary.py``).

Coverage is real but partial - not every jurisdiction has ``parcel_geometry``
available, and ``building_geometry`` is rarer still (not every county that
publishes a parcels layer also publishes a building-footprint one). When
``parcel_geometry`` is missing but REData still resolved a parcel (and
therefore its buildings - county GIS footprints plus NY SHPO's CRIS
inventory, see ``plugins.builtin.parcel_buildings``), the convex hull of
those buildings' points stands in for the property boundary: still-real
survey/CRIS-derived geometry, and a much tighter fit than falling through the
rest of the chain to the synthesized default circle. Only a genuine miss -
no parcel at all, or fewer than 3 usable building coordinates - falls back to
the next provider in the chain.
"""

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
    """Reduce a possibly-multi-shell result to the single largest polygon.

    Deliberately returns the ``MultiPolygon``'s own element directly rather
    than re-wrapping it in ``Polygon(...)`` - unlike ``LineString``/``Point``,
    Django's ``Polygon`` constructor has no "copy an existing Polygon"
    overload, and passing one in raises (confirmed - not a hypothetical).
    """
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        candidates = [polygon for polygon in geom if isinstance(polygon, Polygon)]
        return max(candidates, key=lambda polygon: polygon.area) if candidates else None
    return None


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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Unused - REData is looked up strictly by coordinate.

        Returns:
            ``{"property": ..., "building": ...}``, both possibly None. An
            empty dict (rather than a dict of Nones) when REData isn't
            configured for this install at all, so the chain doesn't even
            log a skipped-provider warning for an intentionally-unused
            integration.
        """
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
            property_polygon = self._buildings_convex_hull(gateway, payload.get("uuid"))

        return {
            "property": property_polygon,
            "building": geojson_polygon_to_geos(payload.get("building_geometry")),
        }

    def _buildings_convex_hull(self, gateway: RedataGateway, parcel_uuid: str | None) -> Polygon | None:
        """Approximate the property boundary as the convex hull of the parcel's own buildings.

        A jurisdiction that never digitized a parcel-boundary shapefile can
        still publish individual building locations (county GIS or NY SHPO's
        CRIS inventory), since those only need a point each. When that's the
        case, the buildings REData already resolved for this parcel are a far
        tighter, still-real boundary approximation than falling through the
        rest of the provider chain to the synthesized default circle.

        Args:
            gateway: The already-constructed ``RedataGateway`` to reuse - the
                parcel lookup and this buildings lookup are for the same
                parcel, no need to re-resolve credentials/base URL.
            parcel_uuid: The parcel's REData uuid, or None if the parcel
                lookup didn't resolve one (e.g. no parcel at this coordinate
                at all).

        Returns:
            A convex-hull ``Polygon`` around the parcel's building
            coordinates, or None when there's no uuid, fewer than 3 usable
            coordinates, the points are collinear (the hull degenerates to a
            line or point), or the buildings lookup itself failed.
        """
        if not parcel_uuid:
            return None
        try:
            buildings = gateway.lookup_buildings(parcel_uuid)
        except PropertyRecordsUnavailableError as exc:
            logger.debug("REData buildings lookup unavailable for parcel %s: %s", parcel_uuid, exc)
            return None

        points: list[Point] = []
        for building in buildings:
            latitude, longitude = building.get("latitude"), building.get("longitude")
            if latitude is None or longitude is None:
                continue
            points.append(Point(float(longitude), float(latitude), srid=4326))
        if len(points) < 3:
            return None

        hull = MultiPoint(points, srid=4326).convex_hull
        return hull if isinstance(hull, Polygon) else None
