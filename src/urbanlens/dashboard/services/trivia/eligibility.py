"""Question eligibility for a Trivia session.

Reuses SpotGuessr's "pinned by every participant" location rule outright
(``services.spotguessr.eligibility.eligible_locations``) - that's a
Location/Pin concept unrelated to the game played on top of it. This module
adds the Trivia-specific second filter: an approved, in-rotation question
about one of those locations, with deterministic questions materialized on
demand for each candidate location as it's considered (see
``services.trivia.deterministic``), so a freshly-pinned location with no
question rows yet still becomes eligible the first time it's checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionStatus
from urbanlens.dashboard.services.spotguessr.eligibility import eligible_locations as _pinned_by_all
from urbanlens.dashboard.services.trivia import deterministic
from urbanlens.dashboard.services.trivia.voting import effective_score

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import GEOSGeometry
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def eligible_questions(
    profiles: Iterable[Profile],
    *,
    geo_bounds: GEOSGeometry | None = None,
    exclude_question_ids: Iterable[int] = (),
) -> QuerySet[TriviaQuestion]:
    """Approved, in-rotation questions about a location every profile in ``profiles`` has pinned.

    "In rotation" means ``effective_score(question) >= 0`` - the same
    gate-at-selection-time pattern ``services.media_relevance`` uses for
    photos, evaluated per-question here since the vote weighting (notably
    the passive ``NO_REACTION`` signal) isn't expressible as a single
    queryset ``annotate()``. A question whose score climbs back to
    non-negative later naturally re-enters rotation - no separate permanent-
    retirement state.

    Args:
        profiles: Every participant in the session.
        geo_bounds: Optional polygon/bbox restricting candidates to a
            player-chosen region.
        exclude_question_ids: Questions to exclude outright - already asked
            earlier in this session (no repeats within one playthrough).

    Returns:
        A TriviaQuestion queryset, unevaluated. Empty (``.none()``) when
        ``profiles`` is empty, mirroring ``eligible_locations``.
    """
    candidate_locations = _pinned_by_all(profiles, geo_bounds=geo_bounds)
    for location in candidate_locations:
        deterministic.generate_deterministic_questions(location)

    questions = TriviaQuestion.objects.filter(location__in=candidate_locations, status=TriviaQuestionStatus.APPROVED)
    exclude_ids = list(exclude_question_ids)
    if exclude_ids:
        questions = questions.exclude(pk__in=exclude_ids)

    in_rotation_ids = [question.pk for question in questions.only("pk") if effective_score(question) >= 0]
    return TriviaQuestion.objects.filter(pk__in=in_rotation_ids).select_related("location")


def has_eligible_questions(profiles: Iterable[Profile], *, geo_bounds: GEOSGeometry | None = None) -> bool:
    """Whether ``eligible_questions`` would return anything at all, without materializing it.

    Used as a cheap pre-check before creating a solo session - mirrors
    ``services.spotguessr.eligibility.has_eligible_locations``.
    """
    return eligible_questions(profiles, geo_bounds=geo_bounds).exists()
