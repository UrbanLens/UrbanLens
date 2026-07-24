"""Location selection: difficulty slider + anti-clustering ("feels random").

See ``docs/designs/spotguessr.md`` ("Difficulty slider", "'Feels random'
selection") for the rules this encodes.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from django.contrib.gis.measure import D

from urbanlens.dashboard.models.spotguessr.model import DEFAULT_RATING, LocationModeRating

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location

#: See docs/designs/spotguessr.md's config table - keep these in sync.
MIN_LOCATION_RATING = 1000.0
MAX_LOCATION_RATING = 2000.0
DIFFICULTY_BANDWIDTH = 200.0
MIN_GAMES_FOR_DIFFICULTY_WEIGHTING = 5
MIN_SEPARATION_KM = 0.5


def target_rating_for_difficulty(difficulty: float) -> float:
    """Map a 0.0 (easiest) - 1.0 (hardest) difficulty slider to a target display-scale rating."""
    clamped = max(0.0, min(1.0, difficulty))
    return MIN_LOCATION_RATING + clamped * (MAX_LOCATION_RATING - MIN_LOCATION_RATING)


def pick_next_location(
    candidates: QuerySet[Location],
    *,
    mode: str,
    difficulty: float,
    previous_location: Location | None,
) -> Location | None:
    """Weighted-random pick from ``candidates`` (already eligibility-filtered, not yet used this session).

    Args:
        candidates: Eligible locations for this round.
        mode: The SpotGuessrMode this round is being generated for - each
            mode has its own difficulty rating for the same location.
        difficulty: 0.0 (easiest) - 1.0 (hardest) slider value.
        previous_location: The prior round's location, if any - used only
            for the anti-clustering proximity exclusion, which is relaxed
            (never the eligibility/no-repeat rules the caller already
            applied) if it would empty the pool.

    Returns:
        The chosen Location, or None if ``candidates`` is empty.
    """
    pool = list(candidates)
    if not pool:
        return None

    if previous_location is not None and previous_location.point is not None:
        separated = list(candidates.filter(point__distance_gte=(previous_location.point, D(km=MIN_SEPARATION_KM))))
        if separated:
            pool = separated

    target_rating = target_rating_for_difficulty(difficulty)
    ratings_by_location_id = {rating.location_id: rating for rating in LocationModeRating.objects.filter(location__in=pool, mode=mode)}
    weights = [_difficulty_weight(ratings_by_location_id.get(location.pk), target_rating) for location in pool]
    if sum(weights) <= 0:
        return random.choice(pool)  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive
    return random.choices(pool, weights=weights, k=1)[0]  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive


def _difficulty_weight(rating: LocationModeRating | None, target_rating: float) -> float:
    """Gaussian kernel weight; locations without enough game history stay neutral (never excluded for lack of data)."""
    if rating is None or rating.games_played < MIN_GAMES_FOR_DIFFICULTY_WEIGHTING:
        location_rating = DEFAULT_RATING
    else:
        location_rating = rating.rating
    return math.exp(-((location_rating - target_rating) ** 2) / (2 * DIFFICULTY_BANDWIDTH**2))
