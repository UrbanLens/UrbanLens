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
