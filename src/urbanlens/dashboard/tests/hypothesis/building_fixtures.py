"""Metre-scale building records around a fixed campus point, in REData's reconciled shape."""

from __future__ import annotations

import math
from typing import Any

CAMPUS_LAT, CAMPUS_LNG = 41.73328, -73.92812
_METRE_LAT = 1 / 111_320
_METRE_LNG = 1 / (111_320 * math.cos(math.radians(CAMPUS_LAT)))


def offset(north_m: float, east_m: float) -> tuple[float, float]:
    """The ``(latitude, longitude)`` this many metres north and east of the campus point."""
    return CAMPUS_LAT + north_m * _METRE_LAT, CAMPUS_LNG + east_m * _METRE_LNG


def ring(corners: list[tuple[float, float]]) -> dict[str, Any]:
    """A GeoJSON Polygon through ``(north_m, east_m)`` corners, closed."""
    coordinates = [[offset(north, east)[1], offset(north, east)[0]] for north, east in corners]
    return {"type": "Polygon", "coordinates": [[*coordinates, coordinates[0]]]}


def rect(north_m: float, east_m: float, width_m: float, height_m: float) -> dict[str, Any]:
    """A GeoJSON rectangle centred ``north_m``/``east_m`` from the campus point."""
    half_w, half_h = width_m / 2, height_m / 2
    return ring(
        [
            (north_m - half_h, east_m - half_w),
            (north_m - half_h, east_m + half_w),
            (north_m + half_h, east_m + half_w),
            (north_m + half_h, east_m - half_w),
        ]
    )


def record(ref: str, north_m: float, east_m: float, *, geometry: dict | None = None, **extra: Any) -> dict[str, Any]:
    """One on-property building record; with ``geometry``, the point is that footprint's centroid, as REData reports it."""
    latitude, longitude = offset(north_m, east_m)
    if geometry is not None:
        import json

        from django.contrib.gis.geos import GEOSGeometry

        centroid = GEOSGeometry(json.dumps(geometry), srid=4326).centroid
        latitude, longitude = centroid.y, centroid.x
    return {
        "ref": ref,
        "name": "",
        "latitude": latitude,
        "longitude": longitude,
        "geometry": geometry,
        "is_on_property": True,
        "sources": [{"source": ref.split(":", 1)[0]}],
        **extra,
    }


def parcel_square(half_size_m: float = 220.0) -> dict[str, Any]:
    """A square parcel centred on the campus point."""
    return rect(0, 0, half_size_m * 2, half_size_m * 2)
