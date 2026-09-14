"""Shared GeoJSON <-> GEOS geometry helpers used by boundary drawing and PinList smart-membership bounding polygons, so both features parse/serialize polygons identically."""

from __future__ import annotations

import json

from django.contrib.gis.gdal import GDALException
from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Polygon


class InvalidPolygonGeoJSONError(ValueError):
    """Base for every way a submitted geometry can fail to be a usable polygon."""


class GeoJSONParseError(InvalidPolygonGeoJSONError):
    """The payload isn't parseable geometry at all - malformed JSON, an unknown ``type``, or a shape GEOS/GDAL otherwise rejects."""


class NotPolygonalGeometryError(InvalidPolygonGeoJSONError):
    """The payload parsed to a real geometry, but not a Polygon or MultiPolygon."""


class EmptyPolygonGeometryError(InvalidPolygonGeoJSONError):
    """The payload parsed to a Polygon/MultiPolygon with no coordinates."""


class TooComplexGeometryError(InvalidPolygonGeoJSONError):
    """The payload parsed, but is larger than one drawn region may be.

    A subclass rather than a new hierarchy, because every caller already turns
    :class:`InvalidPolygonGeoJSONError` into a 400 and a new failure mode here
    should not arrive as a 500 instead.
    """


#: Most components one drawn region may have. The count arrives in a POST body and
#: every component is unioned inside the request. Far above what anyone draws by
#: hand.
MAX_REGION_POLYGONS = 200

#: Most vertices one drawn region may carry in total. A component cap alone would
#: move the cost rather than remove it: a single polygon with a hundred thousand
#: points is one cheap `len()` and one expensive `intersects()`.
MAX_REGION_VERTICES = 50_000


def _refuse_if_too_complex(geom: MultiPolygon) -> None:
    """Refuse a region larger than the dissolve can be asked to handle.

    Args:
        geom: The parsed multipolygon.

    Raises:
        TooComplexGeometryError: Too many components, or too many vertices.
    """
    if len(geom) > MAX_REGION_POLYGONS:
        raise TooComplexGeometryError(f"Region has {len(geom)} components, above the {MAX_REGION_POLYGONS} one region may have")
    vertices = sum(polygon.num_points for polygon in geom)
    if vertices > MAX_REGION_VERTICES:
        raise TooComplexGeometryError(f"Region has {vertices} vertices, above the {MAX_REGION_VERTICES} one region may have")


def geometry_to_geojson(geom) -> dict | None:
    """Serialize a GEOS geometry to a GeoJSON dict, or None."""
    return json.loads(geom.geojson) if geom else None


def parse_multipolygon_geojson(polygon_geojson: dict) -> MultiPolygon:
    """Parse a GeoJSON geometry into a MultiPolygon.

    Args:
        polygon_geojson: A GeoJSON Polygon or MultiPolygon geometry dict.

    Returns:
        The parsed geometry, coerced to MultiPolygon.

    Raises:
        GeoJSONParseError: The payload isn't valid GeoJSON/geometry at all.
        NotPolygonalGeometryError: It parsed, but isn't a Polygon or MultiPolygon.
        EmptyPolygonGeometryError: It parsed to a Polygon/MultiPolygon with no coordinates.
        TooComplexGeometryError: It parsed, but carries more components or
            vertices than one region may.
    """
    try:
        geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
    except (GDALException, GEOSException, TypeError, ValueError) as exc:
        # GDALException, not just GEOSException: GEOSGeometry parses GeoJSON through OGR, so most
        # malformed input - a bare `{}`, an unknown `type`, a `Polygon` with no coordinates -
        # surfaces as GDALException, which is not a GEOSException subclass.
        # Every caller handles InvalidPolygonGeoJSONError (of which this is a subclass) as a 400, so
        raise GeoJSONParseError(f"GEOSGeometry rejected the submitted GeoJSON: {exc!r}") from exc
    if isinstance(geom, Polygon):
        geom = MultiPolygon(geom, srid=geom.srid)
    if not isinstance(geom, MultiPolygon):
        raise NotPolygonalGeometryError(f"Parsed to a {geom.geom_type}, not a Polygon or MultiPolygon")
    if geom.empty:
        # `{"type": "Polygon", "coordinates": []}` parses cleanly into an empty geometry.
        # Storing it is worse than rejecting it: `dissolve_polygons` below documents the same trap -
        # an empty polygon in a `__within` lookup matches zero rows rather than imposing no
        # restriction - and a boundary row holding one draws nothing while reading as "set".
        raise EmptyPolygonGeometryError("Parsed geometry has no coordinates")
    # Here rather than in dissolve_polygons: this is the door every client-
    # supplied geometry comes through, and a ceiling checked after something has
    # already stored the shape is a ceiling on the wrong side of the write.
    _refuse_if_too_complex(geom)
    return geom


def dissolve_polygons(polygons: list[Polygon]) -> MultiPolygon:
    """Merge any polygons that intersect (overlap, touch, or contain) into single components.
    Chained overlaps (A intersects B, B intersects C, A does not intersect C) still fully merge into one component, because after A+B are unioned the resulting shape contains B's footprint and therefore does intersect C.

    Args:
        polygons: GEOS Polygons, all in the same SRID (4326).

    Returns:
        A MultiPolygon whose components are pairwise non-intersecting."""
    if not polygons:
        return MultiPolygon([], srid=4326)

    merged = MultiPolygon(polygons, srid=4326).unary_union
    if isinstance(merged, MultiPolygon):
        flat = [sub for sub in merged if isinstance(sub, Polygon)]
    elif isinstance(merged, Polygon):
        flat = [merged]
    else:
        flat = []
    return MultiPolygon(flat, srid=4326)
