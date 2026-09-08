"""Shared GeoJSON <-> GEOS geometry helpers used by boundary drawing and PinList
smart-membership bounding polygons, so both features parse/serialize polygons
identically.
"""

from __future__ import annotations

import json

from django.contrib.gis.gdal import GDALException
from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Polygon


class InvalidPolygonGeoJSONError(ValueError):
    """Base for every way a submitted geometry can fail to be a usable polygon.

    ``message`` is for logs, not a response: a caller's HTTP-facing code
    should catch a specific subclass below (or this base class as a fallback)
    and author its own user-facing text, rather than relaying ``message`` -
    that keeps a future raise site here from being able to smuggle unreviewed
    (and possibly GEOS/GDAL-internals-bearing) text into a response just by
    adding a new ``raise``.
    """


class GeoJSONParseError(InvalidPolygonGeoJSONError):
    """The payload isn't parseable geometry at all - malformed JSON, an unknown
    ``type``, or a shape GEOS/GDAL otherwise rejects.
    """


class NotPolygonalGeometryError(InvalidPolygonGeoJSONError):
    """The payload parsed to a real geometry, but not a Polygon or MultiPolygon."""


class EmptyPolygonGeometryError(InvalidPolygonGeoJSONError):
    """The payload parsed to a Polygon/MultiPolygon with no coordinates."""


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
    """
    try:
        geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
    except (GDALException, GEOSException, TypeError, ValueError) as exc:
        # GDALException, not just GEOSException: GEOSGeometry parses GeoJSON
        # through OGR, so most malformed input - a bare `{}`, an unknown `type`,
        # a `Polygon` with no coordinates - surfaces as GDALException, which is
        # not a GEOSException subclass. Every caller handles
        # InvalidPolygonGeoJSONError (of which this is a subclass) as a 400, so
        # anything escaping here is a 500 instead, on the public API as well as
        # the drawing editors.
        raise GeoJSONParseError(f"GEOSGeometry rejected the submitted GeoJSON: {exc!r}") from exc
    if isinstance(geom, Polygon):
        geom = MultiPolygon(geom, srid=geom.srid)
    if not isinstance(geom, MultiPolygon):
        raise NotPolygonalGeometryError(f"Parsed to a {geom.geom_type}, not a Polygon or MultiPolygon")
    if geom.empty:
        # `{"type": "Polygon", "coordinates": []}` parses cleanly into an empty
        # geometry. Storing it is worse than rejecting it: `dissolve_polygons`
        # below documents the same trap - an empty polygon in a `__within`
        # lookup matches zero rows rather than imposing no restriction - and a
        # boundary row holding one draws nothing while reading as "set".
        raise EmptyPolygonGeometryError("Parsed geometry has no coordinates")
    return geom


def dissolve_polygons(polygons: list[Polygon]) -> MultiPolygon:
    """Merge any polygons that intersect (overlap, touch, or contain) into single components.

    Runs pairwise unions until no two remaining components intersect. Chained
    overlaps (A intersects B, B intersects C, A does not intersect C) still
    fully merge into one component, because after A+B are unioned the
    resulting shape contains B's footprint and therefore does intersect C.

    Args:
        polygons: GEOS Polygons, all in the same SRID (4326).

    Returns:
        A MultiPolygon whose components are pairwise non-intersecting. Empty
        input yields an empty MultiPolygon - callers must treat that as "no
        geometry" and drop the criteria key entirely rather than storing an
        empty-but-truthy geometry (an empty polygon in a `__within` lookup
        would match zero rows instead of imposing no restriction).
    """
    if not polygons:
        return MultiPolygon([], srid=4326)

    # union() returns the general GEOSGeometry type (not narrowed to
    # Polygon | MultiPolygon), so clusters has to be typed that broadly too.
    clusters: list[GEOSGeometry] = list(polygons)
    merged_any = True
    while merged_any:
        merged_any = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if clusters[i].intersects(clusters[j]):
                    clusters[i] = clusters[i].union(clusters[j])
                    del clusters[j]
                    merged_any = True
                    break
            if merged_any:
                break
    flat: list[Polygon] = []
    for geom in clusters:
        if isinstance(geom, MultiPolygon):
            flat.extend(sub for sub in geom if isinstance(sub, Polygon))
        elif isinstance(geom, Polygon):
            flat.append(geom)
    return MultiPolygon(flat, srid=4326)
