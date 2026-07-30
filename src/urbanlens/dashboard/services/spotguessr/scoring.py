"""Round scoring: point-vs-boundary distance, and the points curve.

See ``docs/designs/spotguessr.md`` ("Scoring: point vs. boundary distance",
"Points") for the rules this encodes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point

from urbanlens.dashboard.services.spotguessr.distance import geodesic_distance_meters, location_boundary_polygon

if TYPE_CHECKING:
    from datetime import date

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location

#: See docs/designs/spotguessr.md's config table - keep these in sync.
MAX_ROUND_POINTS = 5000
MAX_DATE_POINTS = 1000
DATE_DECAY_DAYS = 180.0

#: points_for_distance()'s two-component blend: a fast near-field decay
#: (rewards "a few blocks" precision) plus a slow city-scale decay (keeps
#: "same city" meaningfully non-zero) - see docs/designs/spotguessr.md's
#: Points section for the full rationale and sample values.
NEAR_DECAY_KM = 1.5
CITY_DECAY_KM = 40.0
NEAR_WEIGHT = 0.65


@dataclass(frozen=True)
class RoundTarget:
    """What a round's guess is actually scored against, resolved once at round creation."""

    is_point: bool
    #: A Point when ``is_point``, else the location's effective boundary polygon.
    geometry: object


def resolve_target(location: Location, image: Image | None) -> RoundTarget:
    """Decide whether a round scores by point or boundary distance.

    A photo with its own coordinates represents a specific *point*. A photo
    with none - or no photo at all, e.g. a future Named Place round -
    represents the location itself, scored against its boundary (0 distance
    anywhere inside it).
    """
    if image is not None and image.latitude is not None and image.longitude is not None:
        point = Point(float(image.longitude), float(image.latitude), srid=4326)
        return RoundTarget(is_point=True, geometry=point)
    return RoundTarget(is_point=False, geometry=location_boundary_polygon(location))


def street_view_target(location: Location) -> RoundTarget:
    """Street View mode's target: the location's own point.

    There is no Image row to carry a more specific coordinate - Street View
    imagery is definitionally centered on the location's own point, so
    distance behaves exactly like a coordinate-bearing photo (see
    docs/designs/spotguessr.md's "Street View mode").
    """
    point = Point(float(location.longitude), float(location.latitude), srid=4326)
    return RoundTarget(is_point=True, geometry=point)


def distance_for_guess(location: Location, guess_point: Point, *, target_is_point: bool, target_point: Point | None) -> float:
    """Geodesic distance in meters from ``guess_point`` to a round's target.

    Point-based rounds use the round's coordinate snapshot
    (``target_point``). Boundary-based rounds resolve the location's
    *current* boundary live - boundaries are community-maintained and get
    more accurate over time, so an old round shouldn't freeze a stale one.
    """
    target = target_point if target_is_point else location_boundary_polygon(location)
    if target is None:
        # A location always has coordinates, so location_boundary_polygon()
        # always has at least the circle fallback to return - and a
        # point-target round always has target_point set at creation time
        # (see GameRound.target_point's docstring). Either branch returning
        # None means an invariant broke upstream, not a normal "no data" case.
        raise ValueError("Round has no resolvable scoring target.")
    return geodesic_distance_meters(location, guess_point, target)


def points_for_distance(
    distance_meters: float,
    *,
    near_decay_km: float = NEAR_DECAY_KM,
    city_decay_km: float = CITY_DECAY_KM,
    near_weight: float = NEAR_WEIGHT,
    max_points: int = MAX_ROUND_POINTS,
) -> int:
    """Two-component exponential-decay points curve.

    A single exponential makes anything past ~10-15km read as zero, which
    feels unfairly harsh - guessing the right city, or even just getting
    within a few blocks, should still feel like progress. Blending a fast
    near-field decay (rewards precision) with a slow city-scale decay
    (keeps "same city" meaningfully non-zero) gives: full points only at
    (or inside) the target boundary, "a few blocks off" reading as
    excellent, "same city" reading as a reasonable partial score, and only
    genuinely distant guesses trailing off toward zero.
    """
    distance_km = max(distance_meters, 0.0) / 1000.0
    near = math.exp(-distance_km / near_decay_km)
    city = math.exp(-distance_km / city_decay_km)
    return round(max_points * (near_weight * near + (1 - near_weight) * city))


def points_for_date_guess(
    guessed_date: date | None,
    actual_date: date | None,
    *,
    decay_days: float = DATE_DECAY_DAYS,
    max_points: int = MAX_DATE_POINTS,
) -> int:
    """Date-guessing bonus points (config.date_guessing_enabled), same decay shape as location scoring."""
    if guessed_date is None or actual_date is None:
        return 0
    days_off = abs((guessed_date - actual_date).days)
    return round(max_points * math.exp(-days_off / decay_days))
