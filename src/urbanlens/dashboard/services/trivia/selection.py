"""Question selection: difficulty slider weighting against TriviaQuestionRating.

Mirrors ``services.spotguessr.selection``'s difficulty-weighting pattern
(Gaussian kernel against a rating, neutral default for content without
enough game history) but applied per-question rather than per-location: the
same location can host questions of very different difficulty (a building's
year built vs. a parcel's building count), so difficulty is a question-level
knob here, not a location-level one. Unlike SpotGuessr, there's no spatial
"feels random" anti-clustering rule - a text question doesn't have the
same back-to-back-same-spot repetitiveness a map guess does, so the
within-session exclusion in ``services.trivia.eligibility`` (never repeat an
already-asked question) is the only anti-repeat rule needed.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.spotguessr.model import DEFAULT_RATING
from urbanlens.dashboard.models.trivia.model import TriviaQuestionRating

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.trivia.model import TriviaQuestion

#: Same config shape as ``services.spotguessr.selection`` - see that module
#: for the rationale behind each constant.
MIN_QUESTION_RATING = 1000.0
MAX_QUESTION_RATING = 2000.0
DIFFICULTY_BANDWIDTH = 200.0
MIN_GAMES_FOR_DIFFICULTY_WEIGHTING = 5


def target_rating_for_difficulty(difficulty: float) -> float:
    """Map a 0.0 (easiest) - 1.0 (hardest) difficulty slider to a target display-scale rating."""
    clamped = max(0.0, min(1.0, difficulty))
    return MIN_QUESTION_RATING + clamped * (MAX_QUESTION_RATING - MIN_QUESTION_RATING)


def pick_next_question(candidates: QuerySet[TriviaQuestion], *, difficulty: float) -> TriviaQuestion | None:
    """Weighted-random pick from ``candidates`` (already eligibility-filtered, not yet asked this session).

    Args:
        candidates: Eligible questions for this round.
        difficulty: 0.0 (easiest) - 1.0 (hardest) slider value.

    Returns:
        The chosen TriviaQuestion, or None if ``candidates`` is empty.
    """
    pool = list(candidates)
    if not pool:
        return None

    target_rating = target_rating_for_difficulty(difficulty)
    ratings_by_question_id = {rating.question_id: rating for rating in TriviaQuestionRating.objects.filter(question__in=pool)}
    weights = [_difficulty_weight(ratings_by_question_id.get(question.pk), target_rating) for question in pool]
    if sum(weights) <= 0:
        return random.choice(pool)  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive
    return random.choices(pool, weights=weights, k=1)[0]  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive


def _difficulty_weight(rating: TriviaQuestionRating | None, target_rating: float) -> float:
    """Gaussian kernel weight; questions without enough game history stay neutral (never excluded for lack of data)."""
    if rating is None or rating.games_played < MIN_GAMES_FOR_DIFFICULTY_WEIGHTING:
        question_rating = DEFAULT_RATING
    else:
        question_rating = rating.rating
    return math.exp(-((question_rating - target_rating) ** 2) / (2 * DIFFICULTY_BANDWIDTH**2))
