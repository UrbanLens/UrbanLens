"""Trivia question vote recording and scoring.

Mirrors ``services.spotguessr.relevance`` (recording) + ``services.media.media_relevance``
(weighted scoring), collapsed into one module: Trivia has a single vote table
as its only signal, unlike SpotGuessr's photo relevance which blends an
in-game event log with a separate wiki-vote table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Count

from urbanlens.dashboard.models.trivia.model import TriviaQuestionVote, TriviaQuestionVoteKind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trivia.model import TriviaQuestion

#: Kinds a player may explicitly record, as opposed to NO_REACTION, which is
#: only ever backfilled server-side (see ``backfill_no_reaction``).
EXPLICIT_KINDS = (TriviaQuestionVoteKind.UPVOTE, TriviaQuestionVoteKind.DOWNVOTE, TriviaQuestionVoteKind.REPORT)

#: Deliberately unlike SpotGuessr's photo-relevance weights (where a downvote
#: is a near-zero token weight): the Trivia spec explicitly wants downvotes
#: alone able to retire a question ("significantly downvoted questions won't
#: be asked anymore"), so DOWNVOTE carries real weight here, not just a
#: tie-breaker. REPORT still outweighs a downvote - a report is "this is a
#: problem," not just "I didn't enjoy this one."
UPVOTE_WEIGHT = 1.0
DOWNVOTE_WEIGHT = -1.0
REPORT_WEIGHT = -3.0
#: Exact value from the Trivia spec: a question shown with no explicit
#: reaction still earns a small passive-positive signal, letting a
#: never-voted question bootstrap a non-negative score.
NO_REACTION_WEIGHT = 0.05

_WEIGHTS = {
    TriviaQuestionVoteKind.UPVOTE: UPVOTE_WEIGHT,
    TriviaQuestionVoteKind.DOWNVOTE: DOWNVOTE_WEIGHT,
    TriviaQuestionVoteKind.REPORT: REPORT_WEIGHT,
    TriviaQuestionVoteKind.NO_REACTION: NO_REACTION_WEIGHT,
}


def record_vote(question: TriviaQuestion, profile: Profile, kind: str) -> TriviaQuestionVote:
    """Record (or change) ``profile``'s explicit reaction to ``question``.

    Always overwrites whatever was previously recorded for this
    ``(question, profile)`` pair - including a prior ``NO_REACTION``
    backfill, or an earlier change of mind.

    Args:
        question: The question being voted on.
        profile: The voting participant.
        kind: One of ``EXPLICIT_KINDS``.
    """
    vote, _ = TriviaQuestionVote.objects.update_or_create(question=question, profile=profile, defaults={"kind": kind})
    return vote


def backfill_no_reaction(question: TriviaQuestion, profiles: Iterable[Profile]) -> None:
    """Record a weak default-positive signal for every participant who never explicitly voted on ``question``.

    Called once a round is fully revealed (see ``services.trivia.session.submit_answer``).
    Uses ``get_or_create`` (not ``update_or_create``) so this can never
    clobber an explicit reaction a fast player already submitted before the
    round finished for everyone else.
    """
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
