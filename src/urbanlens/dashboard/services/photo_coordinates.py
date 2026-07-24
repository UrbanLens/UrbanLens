"""Turning anonymized SpotGuessr coordinate guesses into an estimated photo position.

See ``services.spotguessr.photo_coordinates`` for where guesses actually get
recorded; this module only turns the accumulated ``PhotoCoordinateGuess``
rows for one photo into a single cached estimate on ``Image``.
"""

from __future__ import annotations

from django.utils import timezone

from urbanlens.dashboard.models.spotguessr.model import PhotoCoordinateGuess

#: Below this many correct guesses, a photo's position is simply unknown -
#: not enough signal to be worth caching or showing on a map at all.
MIN_GUESSES_FOR_ESTIMATE = 5

#: Below this many correct guesses, skip the outlier trim entirely - there's
#: not enough data for "drop the farthest share" to mean anything
#: statistically; just average everything.
MIN_GUESSES_FOR_OUTLIER_TRIM = 10

#: Loose, cheap outlier rejection: drop this share of guesses (the ones
#: farthest from the centroid) before recomputing the average. Not a
#: rigorous statistical test - just enough to keep one wild misclick from
#: skewing a small sample, at minimal cost.
OUTLIER_TRIM_FRACTION = 0.15


def recompute_estimated_coordinates(image_id: int) -> None:
    """Recompute and cache one photo's estimated position from its correct guesses so far.

    Called after every new correct guess (see
    ``services.spotguessr.photo_coordinates.record_guess``). Recomputes from
    the full current set each time rather than maintaining a running
    average: per-photo guess volume is small enough that this stays cheap
    (the cost scales with guesses on *this* photo, never with the size of
    the guess table overall), and it keeps the outlier trim's boundary
    correct as more data arrives instead of needing separate bookkeeping to
    "undo" an old point's contribution.

    A no-op below ``MIN_GUESSES_FOR_ESTIMATE`` correct guesses - deliberately
    leaves any previously-cached estimate in place rather than clearing it,
    though in practice a photo's correct-guess count never decreases, so
    that situation cannot actually arise.

    Args:
        image_id: pk of the ``Image`` being estimated - not the object
            itself, since callers already have just the id and this never
            needs to touch the rest of the row until the final update.
    """
    # Read via the deserialized GEOS Point's own .y/.x rather than an
    # ST_X/ST_Y annotation - guess_point is a `geography` column, and
    # PostGIS's X()/Y() functions expect `geometry`, not `geography`. Fine
    # either way at this scale: per-photo guess volume is small, so fetching
    # full Point objects and reading coordinates in Python costs nothing
    # extra worth optimizing away.
    points = [
        (point.y, point.x)
        for point in PhotoCoordinateGuess.objects.filter(image_id=image_id, is_correct=True).values_list("guess_point", flat=True)
    ]
    if len(points) < MIN_GUESSES_FOR_ESTIMATE:
        return

    if len(points) >= MIN_GUESSES_FOR_OUTLIER_TRIM:
        points = _drop_farthest(points)

    avg_lat = sum(lat for lat, _lng in points) / len(points)
    avg_lng = sum(lng for _lat, lng in points) / len(points)

    from urbanlens.dashboard.models.images.model import Image

    Image.objects.filter(pk=image_id).update(estimated_latitude=avg_lat, estimated_longitude=avg_lng, updated=timezone.now())


def _drop_farthest(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop the ``OUTLIER_TRIM_FRACTION`` of points farthest from the centroid of all of them.

    Distance is plain (lat, lng) degree distance, not a geodesic one - a
    location's boundary is small enough (building/property scale) that this
    is a fine approximation for a deliberately loose trim, and far cheaper
    than a real haversine/PostGIS distance per point.
    """
    centroid_lat = sum(lat for lat, _lng in points) / len(points)
    centroid_lng = sum(lng for _lat, lng in points) / len(points)
    ranked = sorted(points, key=lambda point: (point[0] - centroid_lat) ** 2 + (point[1] - centroid_lng) ** 2)
    keep = max(MIN_GUESSES_FOR_ESTIMATE, len(points) - round(len(points) * OUTLIER_TRIM_FRACTION))
    return ranked[:keep]
