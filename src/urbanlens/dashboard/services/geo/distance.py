"""Great-circle distance, in one place.

Five modules had grown their own haversine - profile map centring, public-pin
clustering, consensus answer scoring, Overture boundary matching, and markup
geometry - each an independent copy of the same eight lines. They were verified
to agree exactly (0.00m spread over short, long, and antimeridian-crossing pairs)
before being pointed here, so this is a consolidation and not a behaviour change.

It exists because duplicated geometry primitives in this codebase have already
drifted twice: four separate longitude averages, one of which put a map centre in
the Atlantic, and a "nearest pin" lookup that ordered by a geometry column while a
correct distance-ordered helper sat a few lines away in the same file. Nothing was
wrong with any of these five - the point is that the sixth copy is where the next
bug goes.

Use PostGIS (``Distance``, ``distance_lte``) for anything the database can answer;
this is for distances between values already in memory.
"""

from __future__ import annotations

import math

#: Mean Earth radius. All five previous copies used this same value.
EARTH_RADIUS_METERS = 6_371_000.0


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS-84 points, in metres.

    Handles the antimeridian without special-casing: the formula works on the
    *difference* between longitudes through a trig function, so 179.99 and
    -179.99 come out 2.2km apart rather than most of the way round the planet.

    Args:
        lat1: First point's latitude in degrees.
        lng1: First point's longitude in degrees.
        lat2: Second point's latitude in degrees.
        lng2: Second point's longitude in degrees.

    Returns:
        Distance in metres.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres.

    Args:
        lat1: First point's latitude in degrees.
        lng1: First point's longitude in degrees.
        lat2: Second point's latitude in degrees.
        lng2: Second point's longitude in degrees.

    Returns:
        Distance in kilometres.
    """
    return haversine_meters(lat1, lng1, lat2, lng2) / 1000.0
