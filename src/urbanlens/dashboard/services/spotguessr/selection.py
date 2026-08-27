"""Location selection: difficulty slider + anti-clustering ("feels random").

See ``docs/designs/drafts/spotguessr.md`` ("Difficulty slider", "'Feels random'
selection") for the rules this encodes.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from django.contrib.gis.measure import D
from django.db.models import Count

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.spotguessr.model import DEFAULT_RATING, LocationModeRating

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location

#: See docs/designs/drafts/spotguessr.md's config table - keep these in sync.
MIN_LOCATION_RATING = 1000.0
MAX_LOCATION_RATING = 2000.0
DIFFICULTY_BANDWIDTH = 200.0
MIN_GAMES_FOR_DIFFICULTY_WEIGHTING = 5
MIN_SEPARATION_KM = 0.5

#: Proxy-seeding saturation points (services.spotguessr.selection._proxy_difficulty_rating) -
#: a location with this many pins/photos or more is treated as "as
#: well-documented as it gets" for the proxy estimate; there's no reward for
#: exceeding it, since the point is only to distinguish "probably easy to
#: recognize" from "nobody's ever seen this place" before real play data exists.
_PROXY_PIN_SATURATION = 20
_PROXY_PHOTO_SATURATION = 10


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

    location_ids = [location.pk for location in pool]
    # Two bulk aggregate queries instead of location.pins.count()/location.images.count()
    # per candidate (see _proxy_difficulty_rating) - with a large pool this loop runs
    # once per attempt of the caller's location-retry loop
    # (session.get_or_create_round), so an O(pool size) query count there compounds
    # into thousands of round trips and was the dominant cause of SpotGuessr /start/
    # being slow-to-timing-out (see "Unit 24/25 (deferred item, now fixed): round
    # generation re-ran the eligibility query per retry" in docs/PROBLEMS-ARCHIVE.md).
    pin_counts = dict(Pin.objects.filter(location_id__in=location_ids).values_list("location_id").annotate(n=Count("pk")).values_list("location_id", "n"))
    photo_counts = dict(Image.objects.filter(location_id__in=location_ids).values_list("location_id").annotate(n=Count("pk")).values_list("location_id", "n"))

    target_rating = target_rating_for_difficulty(difficulty)
    ratings_by_location_id = {rating.location_id: rating for rating in LocationModeRating.objects.filter(location__in=pool, mode=mode)}
    weights = [
        _difficulty_weight(
            location,
            ratings_by_location_id.get(location.pk),
            target_rating,
            pin_count=pin_counts.get(location.pk, 0),
            photo_count=photo_counts.get(location.pk, 0),
        )
        for location in pool
    ]
    if sum(weights) <= 0:
        return random.choice(pool)  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive
    return random.choices(pool, weights=weights, k=1)[0]  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive


def _proxy_difficulty_rating(location: Location, *, pin_count: int | None = None, photo_count: int | None = None) -> float:
    """Estimate a difficulty rating from proxies the database already has, before anyone's ever played it.

    A location with earned play history (``LocationModeRating.games_played``)
    doesn't need this - see ``_difficulty_weight``. Before that history
    exists, every location otherwise looks identically "neutral" (the flat
    ``DEFAULT_RATING``), which makes the difficulty slider a placebo for the
    vast majority of locations under per-user pin pools (see the SpotGuessr
    audit's "difficulty slider is mostly a placebo" finding). How many users
    have pinned a location and how many photos exist for it both proxy
    "well-documented and recognizable" - never assumed *harder* than neutral
    purely for lack of data (mirroring ``_difficulty_weight``'s own "never
    excluded for lack of data" contract), only ever easier as the signal
    strengthens.

    Args:
        location: The candidate location.
        pin_count: Pre-counted ``location.pins.count()``, when the caller has
            already bulk-fetched it for a whole candidate pool (see
            ``pick_next_location``) - avoids a per-location query. Falls back
            to counting it directly here when omitted, so this stays callable
            (and testable) with just a location.
        photo_count: Same idea for ``location.images.count()``.
    """
    if pin_count is None:
        pin_count = location.pins.count()
    if photo_count is None:
        photo_count = location.images.count()
    pin_signal = min(pin_count / _PROXY_PIN_SATURATION, 1.0)
    photo_signal = min(photo_count / _PROXY_PHOTO_SATURATION, 1.0)
    popularity = (pin_signal + photo_signal) / 2.0
    return DEFAULT_RATING - popularity * (DEFAULT_RATING - MIN_LOCATION_RATING)


def _difficulty_weight(location: Location, rating: LocationModeRating | None, target_rating: float, *, pin_count: int | None = None, photo_count: int | None = None) -> float:
    """Gaussian kernel weight, blending toward a proxy-seeded estimate for locations with too little game history.

    A location with at least ``MIN_GAMES_FOR_DIFFICULTY_WEIGHTING`` played
    rounds uses its own earned rating outright. Below that, it blends the
    proxy estimate (``_proxy_difficulty_rating``) with whatever real rating
    exists so far, weighted by how close it is to that threshold - a smooth
    handoff rather than a discontinuous jump the instant the threshold is
    crossed. A location with *zero* games played uses the proxy estimate
    outright, never the flat neutral default.

    Args:
        location: The candidate location.
        rating: This location's earned ``LocationModeRating`` for the round's
            mode, or None if it's never been played.
        target_rating: The difficulty slider's target rating band.
        pin_count: See ``_proxy_difficulty_rating``.
        photo_count: See ``_proxy_difficulty_rating``.
    """
    if rating is not None and rating.games_played >= MIN_GAMES_FOR_DIFFICULTY_WEIGHTING:
        location_rating = rating.rating
    elif rating is not None and rating.games_played:
        proxy_rating = _proxy_difficulty_rating(location, pin_count=pin_count, photo_count=photo_count)
        earned_weight = rating.games_played / MIN_GAMES_FOR_DIFFICULTY_WEIGHTING
        location_rating = proxy_rating * (1 - earned_weight) + rating.rating * earned_weight
    else:
        location_rating = _proxy_difficulty_rating(location, pin_count=pin_count, photo_count=photo_count)
    return math.exp(-((location_rating - target_rating) ** 2) / (2 * DIFFICULTY_BANDWIDTH**2))
