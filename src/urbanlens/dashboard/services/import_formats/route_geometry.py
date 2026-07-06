"""Shared route-geometry helpers - simplification and distance measurement.

Used by both the GPX track/route parser and the Google Takeout semantic-history
activitySegment parser, since both need to turn a raw sequence of (lat, lng)
points into a simplified LineString suitable for storage on Route.path plus a
distance measurement computed from the full-resolution points.
"""

from __future__ import annotations

from typing import NamedTuple

from django.contrib.gis.geos import LineString
from geopy.distance import geodesic
from shapely.geometry import LineString as ShapelyLineString

# ~5m at mid-latitudes. A single fixed tolerance is enough to collapse
# thousands of raw GPS points down to a few hundred for map/timeline display;
# a zoom-adaptive simplification pyramid isn't warranted at this data scale.
SIMPLIFY_TOLERANCE_DEGREES = 0.00005


class RouteGeometry(NamedTuple):
    """Result of simplifying and measuring a raw point sequence."""

    path: LineString
    distance_meters: float
    raw_point_count: int
    simplified_point_count: int


def simplify_and_measure(points: list[tuple[float, float]]) -> RouteGeometry:
    """Simplify a raw (lat, lng) point sequence and measure its true distance.

    Args:
        points: Raw ``(latitude, longitude)`` points in recording order.
            Must contain at least 2 points.

    Returns:
        RouteGeometry with the simplified path (a GeoDjango LineString, ready
        to assign to ``Route.path``), the cumulative geodesic distance over
        the *raw* points, and the raw/simplified point counts.

    Raises:
        ValueError: If fewer than 2 points are given.
    """
    if len(points) < 2:
        raise ValueError("At least 2 points are required to build a route.")

    distance_meters = sum(geodesic(points[i], points[i + 1]).meters for i in range(len(points) - 1))

    shapely_line = ShapelyLineString([(lng, lat) for lat, lng in points])
    simplified = shapely_line.simplify(SIMPLIFY_TOLERANCE_DEGREES, preserve_topology=False)
    # simplify() can degenerate a nearly-straight line to <2 points in edge cases;
    # fall back to the endpoints so a valid LineString can always be built.
    simplified_coords = list(simplified.coords) if len(simplified.coords) >= 2 else [shapely_line.coords[0], shapely_line.coords[-1]]

    path = LineString(simplified_coords, srid=4326)

    return RouteGeometry(
        path=path,
        distance_meters=distance_meters,
        raw_point_count=len(points),
        simplified_point_count=len(simplified_coords),
    )
