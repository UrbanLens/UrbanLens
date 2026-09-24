"""Area of a WGS-84 geometry that has not been saved, so there is no PostGIS row to measure."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from django.contrib.gis.gdal import GDALException

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry

#: WGS 84 / NSIDC EASE-Grid 2.0 Global: equal-area, so a planar area in it is a true area.
EQUAL_AREA_SRID = 6933


def area_sqm(geometry: GEOSGeometry) -> float:
    """Area in square metres, within a fraction of a percent of PostGIS's geography area.

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
