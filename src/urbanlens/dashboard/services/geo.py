"""Shared GeoJSON <-> GEOS geometry helpers used by boundary drawing and PinList
smart-membership bounding polygons, so both features parse/serialize polygons
identically.
"""

from __future__ import annotations

import json

from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Polygon


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
        ValueError: If the payload isn't valid polygonal GeoJSON.
        TypeError: If the geometry is valid but not polygonal.
    """
    try:
        geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
    except (GEOSException, TypeError, ValueError) as exc:
        raise ValueError("Invalid polygon geometry") from exc
    if isinstance(geom, Polygon):
        geom = MultiPolygon(geom, srid=geom.srid)
    if not isinstance(geom, MultiPolygon):
        raise TypeError("Boundary must be a Polygon or MultiPolygon")
    return geom
