"""Trivia question vote recording and scoring."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Case, Count, DecimalField, OuterRef, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce

from urbanlens.dashboard.models.trivia.model import TriviaQuestionVote, TriviaQuestionVoteKind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trivia.model import TriviaQuestion

#: Kinds a player may explicitly record, as opposed to NO_REACTION, which is
#: only ever backfilled server-side (see ``backfill_no_reaction``).
EXPLICIT_KINDS = (TriviaQuestionVoteKind.UPVOTE, TriviaQuestionVoteKind.DOWNVOTE, TriviaQuestionVoteKind.REPORT)

#: Deliberately unlike SpotGuessr's photo-relevance weights (where a downvote is a near-zero token
#: weight): the Trivia spec explicitly wants downvotes alone able to retire a question
#: ("significantly downvoted questions won't be asked anymore"), so DOWNVOTE carries real weight
#: here, not just a tie-breaker.
UPVOTE_WEIGHT = 1.0
DOWNVOTE_WEIGHT = -1.0
REPORT_WEIGHT = -3.0
#: Exact value from the Trivia spec: a question shown with no explicit
#: reaction still earns a small passive-positive signal, letting a
#: never-voted question bootstrap a non-negative score.
NO_REACTION_WEIGHT = 0.05

_WEIGHTS: dict[str, float] = {
    TriviaQuestionVoteKind.UPVOTE: UPVOTE_WEIGHT,
    TriviaQuestionVoteKind.DOWNVOTE: DOWNVOTE_WEIGHT,
    TriviaQuestionVoteKind.REPORT: REPORT_WEIGHT,
    TriviaQuestionVoteKind.NO_REACTION: NO_REACTION_WEIGHT,
}


def record_vote(question: TriviaQuestion, profile: Profile, kind: str) -> TriviaQuestionVote:
    """Record (or change) ``profile``'s explicit reaction to ``question``.

    Args:
        question: The question being voted on.
        profile: The voting participant.
        kind: One of ``EXPLICIT_KINDS``."""
    vote, _ = TriviaQuestionVote.objects.update_or_create(question=question, profile=profile, defaults={"kind": kind})
    return vote


def backfill_no_reaction(question: TriviaQuestion, profiles: Iterable[Profile]) -> None:
    """Record a weak default-positive signal for every participant who never explicitly voted on ``question``."""
    for profile in profiles:
        TriviaQuestionVote.objects.get_or_create(question=question, profile=profile, defaults={"kind": TriviaQuestionVoteKind.NO_REACTION})


def effective_score(question: TriviaQuestion) -> float:
    """This question's blended vote score - weighted sum of upvote/downvote/report/no-reaction.

    Args:
        question: The question to score.

    Returns:
        The weighted sum described above; 0.0 for a never-voted question.
    """
    counts = TriviaQuestionVote.objects.for_question(question).values("kind").annotate(n=Count("pk"))
    return sum(_WEIGHTS.get(row["kind"], 0.0) * row["n"] for row in counts)


_SCORE_FIELD = DecimalField(max_digits=12, decimal_places=2)


def score_expression() -> Coalesce:
    """:func:`effective_score` as an expression on ``TriviaQuestion``, for filtering many questions in one query.

    Summed as ``numeric``, so a score that lands exactly on a threshold compares exactly.
    """
    weighted = Case(*(When(kind=kind, then=Value(Decimal(str(weight)))) for kind, weight in _WEIGHTS.items()), default=Value(Decimal(0)), output_field=_SCORE_FIELD)
    per_question = TriviaQuestionVote.objects.filter(question=OuterRef("pk")).order_by().values("question").annotate(score=Sum(weighted)).values("score")
    return Coalesce(Subquery(per_question, output_field=_SCORE_FIELD), Value(Decimal(0)), output_field=_SCORE_FIELD)
