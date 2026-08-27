"""Longitude arithmetic that survives the antimeridian.

Longitude wraps at ±180, so the ordinary arithmetic every other coordinate uses
gives nonsense there:

- averaging 179.99 and -179.99 yields **0.0** - a point in the Atlantic, some
  20,000km from either input;
- ``abs(179.99 - -179.99)`` is 359.98, so two points ~1km apart read as being on
  opposite sides of the planet.

Both mistakes were live in this codebase - in fact-evidence centroids, in the
profile's saved map centre, and in the import-failure location guess - and each
one was written independently, which is why these live in one place now. They
return exactly the same answers as the naive arithmetic everywhere except within
a hair of the date line, so adopting them is never a behaviour change for the
99.9% of the planet that never noticed.

Latitude needs none of this: it is clamped to ±90 and does not wrap.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def circular_mean_longitude(longitudes: Sequence[float], weights: Sequence[float] | None = None) -> float:
    """Average longitudes as directions rather than as numbers.

    Args:
        longitudes: Degrees, each in [-180, 180]. Must not be empty.
        weights: Optional per-longitude weights, same length. Defaults to equal
            weighting. A total weight of zero falls back to equal weighting,
            since a zero-weight set has no weighted answer.

    Returns:
        The mean longitude in degrees, in [-180, 180]. When the inputs cancel
        exactly - two antipodal readings have no midpoint - the first input is
        returned rather than an invented value.
    """
    if not longitudes:
        raise ValueError("circular_mean_longitude() requires at least one longitude")

    if weights is None or sum(weights) <= 0:
        weights = [1.0] * len(longitudes)
    total = sum(weights)

    x = sum(math.cos(math.radians(lng)) * weight for lng, weight in zip(longitudes, weights, strict=True)) / total
    y = sum(math.sin(math.radians(lng)) * weight for lng, weight in zip(longitudes, weights, strict=True)) / total
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return longitudes[0]
    return math.degrees(math.atan2(y, x))


def longitude_delta(a: float, b: float) -> float:
    """Shortest angular distance between two longitudes, in degrees.

    Args:
        a: First longitude in degrees.
        b: Second longitude in degrees.

    Returns:
        A value in [0, 180]. ``longitude_delta(179.99, -179.99)`` is 0.02, not
        359.98.
    """
    delta = abs(a - b) % 360.0
    return min(delta, 360.0 - delta)


def normalize_longitude(longitude: float) -> float:
    """Fold a longitude into [-180, 180].

    Map clients report unwrapped bounds when panned across the date line -
    Leaflet's ``getEast()`` can return 181 - while stored coordinates are always
    folded, so the two never match until one side is normalised.

    Args:
        longitude: Degrees, possibly outside [-180, 180].

    Returns:
        The same meridian expressed in [-180, 180]. A bound *on* the line keeps
        its side: any positive input landing on the antimeridian returns +180,
        any negative one returns -180. That has to be decided on the sign of the
        input rather than by special-casing the literal 180, or the same meridian
        normalises two different ways depending on how many times the client
        wrapped it (180 and 540 are one meridian; folding gave +180 and -180).
    """
    folded = (longitude + 180.0) % 360.0 - 180.0
    if folded == -180.0 and longitude > 0.0:
        return 180.0
    return folded


def split_at_antimeridian(geometry):
    """Fold a region that runs past +/-180 into the two halves it really covers.

    Map clients report unwrapped coordinates when the user draws across the date
    line - Leaflet gives a box from 179 to 181 rather than 179 to -179 - while
    stored points are always folded into [-180, 180]. A planar ``__within``
    against the unwrapped polygon therefore misses everything on the far side of
    the line: measured, a region drawn across it matched only the pins west of
    it.

    Splitting at the meridian and translating the overhanging part gives a shape
    that means the same thing in stored coordinates.

    Args:
        geometry: A Polygon or MultiPolygon in SRID 4326.

    Returns:
        The geometry unchanged when it lies within [-180, 180], otherwise a
        MultiPolygon of its two halves.

    Note:
        A polygon whose vertices are already folded but which spans more than
        180 degrees (179 to -179 written literally) is genuinely ambiguous - the
        coordinates say "the long way round" - and is left alone. Clients should
        send the unwrapped form for a region crossing the line.
    """
    from django.contrib.gis.geos import MultiPolygon, Polygon

    min_x, _min_y, max_x, _max_y = geometry.extent
    if min_x >= -180.0 and max_x <= 180.0:
        return geometry

    def clip(geom, west: float, east: float):
        window = Polygon.from_bbox((west, -90.0, east, 90.0))
        window.srid = 4326
        piece = geom.intersection(window)
        return piece if not piece.empty else None

    pieces = []
    inside = clip(geometry, -180.0, 180.0)
    if inside is not None:
        pieces.append(inside)

    # The part beyond the meridian, brought back to the coordinates points are
    # actually stored at.
    if max_x > 180.0:
        overhang = clip(geometry, 180.0, max_x)
        if overhang is not None:
            shifted = overhang.clone()
            shifted.srid = 4326
            pieces.append(_translate_longitude(shifted, -360.0))
    if min_x < -180.0:
        overhang = clip(geometry, min_x, -180.0)
        if overhang is not None:
            shifted = overhang.clone()
            shifted.srid = 4326
            pieces.append(_translate_longitude(shifted, 360.0))

    polygons: list = []
    for piece in pieces:
        if piece.geom_type == "MultiPolygon":
            polygons.extend(list(piece))
        elif piece.geom_type == "Polygon":
            polygons.append(piece)
    if not polygons:
        return geometry
    result = MultiPolygon(polygons)
    result.srid = 4326
    return result


def _translate_longitude(geometry, offset: float):
    """Shift every vertex of a polygon east/west by ``offset`` degrees.

    Args:
        geometry: A Polygon or MultiPolygon in SRID 4326.
        offset: Degrees to add to every x coordinate.

    Returns:
        A new geometry with the shift applied.
    """
    from django.contrib.gis.geos import MultiPolygon, Polygon

    def shift_polygon(polygon):
        rings = [[(x + offset, y) for x, y in ring] for ring in polygon]
        shifted = Polygon(*rings)
        shifted.srid = 4326
        return shifted

    if geometry.geom_type == "MultiPolygon":
        shifted_multi = MultiPolygon([shift_polygon(p) for p in geometry])
        shifted_multi.srid = 4326
        return shifted_multi
    return shift_polygon(geometry)
