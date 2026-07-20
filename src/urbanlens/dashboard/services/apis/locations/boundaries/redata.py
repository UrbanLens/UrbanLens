"""Boundary provider backed by REData's authoritative county parcel/building geometry.

Unlike every other provider in the chain (community-tagged OSM ways,
ML-derived building-footprint datasets), this is survey-grade geometry
straight from the county assessor's own GIS layer - the same data
``plugins.builtin.property_records`` already fetches for the Ownership card,
just consumed for its ``parcel_geometry``/``building_geometry`` instead of its
attribute fields. See ``docs/redata.md`` for the extraction this depends on.

Coverage is real but partial: only jurisdictions REData has a Tier 1 ArcGIS
endpoint configured for populate ``parcel_geometry`` at all (Socrata sources
and any future Tier 2/3 scrape never do), and ``building_geometry`` only when
that jurisdiction *additionally* has a sibling building-footprint layer
configured - most counties that publish a parcels layer don't also publish
one. Falling back to the next provider in the chain when either is missing is
the expected, common case, not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar

from django.contrib.gis.geos import MultiPolygon, Polygon

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, esri_rings_to_polygon
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

        try:
            payload = RedataGateway().lookup_parcel(latitude, longitude)
        except PropertyRecordsUnavailableError as exc:
            logger.debug("REData boundary lookup unavailable for %s: %s", self.service_key, exc)
            return {"property": None, "building": None}

        return {
            "property": esri_rings_to_polygon(payload.get("parcel_geometry")),
            "building": esri_rings_to_polygon(payload.get("building_geometry")),
        }
