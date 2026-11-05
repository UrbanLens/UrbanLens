"""Area of a WGS-84 geometry that has not been saved, so there is no PostGIS row to measure."""

from __future__ import annotations

import math

from django.contrib.gis.gdal import CoordTransform, GDALException, OGRGeometry, SpatialReference
from django.contrib.gis.geos import GEOSException, GEOSGeometry

#: WGS 84 / NSIDC EASE-Grid 2.0 Global: equal-area, so a planar area in it is a true area.
EQUAL_AREA_SRID = 6933


def area_sqm(geometry: GEOSGeometry) -> float:
    """
    Area in square metres, within a fraction of a percent of PostGIS's geography area.

    Args:
        geometry: A polygonal geometry; an unset SRID is read as WGS-84.

    Returns:
        The area in square metres, or infinity when the geometry cannot be projected (a latitude beyond a pole),
        so a ceiling check refuses it rather than raising.
    """
    projected = geometry.clone()
    if projected.srid is None:
        projected.srid = 4326
    try:
        projected.transform(EQUAL_AREA_SRID)
    except GDALException:
        return math.inf
    return float(projected.area)


def buffer_meters(geometry: GEOSGeometry, meters: float) -> GEOSGeometry:
    """
    Grow a WGS-84 geometry outward by a distance on the ground.

    Args:
        geometry: A polygonal geometry; an unset SRID is read as WGS-84.
        meters: How far to grow it, in metres.

    Returns:
        The buffered geometry in WGS-84, or the input unchanged when it cannot be projected.
    """
    try:
        centroid = geometry.centroid
        wgs84 = SpatialReference(4326)
        local = SpatialReference(f"+proj=aeqd +lat_0={centroid.y} +lon_0={centroid.x} +datum=WGS84 +units=m")
        flat = OGRGeometry(geometry.wkt, srs=wgs84)
        flat.transform(CoordTransform(wgs84, local))
        grown = GEOSGeometry(flat.wkt).buffer(meters)
        back = OGRGeometry(grown.wkt, srs=local)
        back.transform(CoordTransform(local, wgs84))
    except (GDALException, GEOSException):
        return geometry
    result = back.geos
    result.srid = geometry.srid or 4326
    return result
