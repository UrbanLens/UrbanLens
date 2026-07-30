"""QuerySets/Managers for Trivia models.

Glicko-2 rating math lives in ``services.spotguessr.glicko2`` (reused
directly); eligibility, question selection, and vote scoring live in
``services.trivia.eligibility``/``selection``/``voting``. These classes only
scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trivia.model import (
        PlayerTriviaRating,
        TriviaAnswer,
        TriviaQuestion,
        TriviaQuestionRating,
        TriviaQuestionVote,
        TriviaRound,
        TriviaSession,
        TriviaSessionChatMessage,
        TriviaSessionParticipant,
    )


class TriviaQuestionQuerySet(abstract.DashboardQuerySet["TriviaQuestion"]):
    """QuerySet for TriviaQuestion."""

    def for_location(self, location: Location) -> TriviaQuestionQuerySet:
        """Restrict to questions about ``location``."""
        return self.filter(location=location)

    def approved(self) -> TriviaQuestionQuerySet:
        """Restrict to questions that passed moderation and are eligible for rotation."""
        from urbanlens.dashboard.models.trivia.model import TriviaQuestionStatus

        return self.filter(status=TriviaQuestionStatus.APPROVED)


class TriviaQuestionManager(abstract.DashboardManager.from_queryset(TriviaQuestionQuerySet)):
    """Manager for TriviaQuestion."""


class TriviaQuestionVoteQuerySet(abstract.DashboardQuerySet["TriviaQuestionVote"]):
    """QuerySet for TriviaQuestionVote."""

    def for_question(self, question: TriviaQuestion) -> TriviaQuestionVoteQuerySet:
        """Every vote recorded for ``question``."""
        return self.filter(question=question)


class TriviaQuestionVoteManager(abstract.DashboardManager.from_queryset(TriviaQuestionVoteQuerySet)):
    """Manager for TriviaQuestionVote."""


class PlayerTriviaRatingQuerySet(abstract.DashboardQuerySet["PlayerTriviaRating"]):
    """QuerySet for PlayerTriviaRating."""

    def for_profile(self, profile: Profile) -> PlayerTriviaRatingQuerySet:
        """Restrict to ``profile``'s own rating row."""
        return self.filter(profile=profile)


class PlayerTriviaRatingManager(abstract.DashboardManager.from_queryset(PlayerTriviaRatingQuerySet)):
    """Manager for PlayerTriviaRating."""

    def get_or_create_for(self, profile: Profile) -> PlayerTriviaRating:
        """Return ``profile``'s Trivia rating row, creating it at the default rating if missing."""
        rating, _ = self.get_or_create(profile=profile)
        return rating


class TriviaQuestionRatingQuerySet(abstract.DashboardQuerySet["TriviaQuestionRating"]):
    """QuerySet for TriviaQuestionRating."""

    def for_question(self, question: TriviaQuestion) -> TriviaQuestionRatingQuerySet:
        """Restrict to ``question``'s own difficulty rating row."""
        return self.filter(question=question)


class TriviaQuestionRatingManager(abstract.DashboardManager.from_queryset(TriviaQuestionRatingQuerySet)):
    """Manager for TriviaQuestionRating."""

    def get_or_create_for(self, question: TriviaQuestion) -> TriviaQuestionRating:
        """Return ``question``'s difficulty rating row, creating it at the default rating if missing."""
        rating, _ = self.get_or_create(question=question)
        return rating


class TriviaSessionQuerySet(abstract.DashboardQuerySet["TriviaSession"]):
    """QuerySet for TriviaSession."""

    def active(self) -> TriviaSessionQuerySet:
        """Restrict to sessions still in progress (lobby or active)."""
        from urbanlens.dashboard.models.trivia.model import TriviaSessionStatus

        return self.filter(status__in=[TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE])

    def for_profile(self, profile: Profile) -> TriviaSessionQuerySet:
        """Restrict to sessions ``profile`` is (or was) a participant in, any status."""
        return self.filter(participants__profile=profile).distinct()

    def stalled(self, *, cutoff: datetime) -> TriviaSessionQuerySet:
        """ACTIVE sessions whose current round was created before ``cutoff`` and still isn't revealed.

        ``get_or_create_round`` never creates a session's next round until
        its prior one is fully revealed, so at most one round per session
        can ever match "unrevealed" at a time - this is always that
        session's current round. Used by the stall-sweep Celery task
        (``tasks.sweep_stalled_trivia_sessions``) to find sessions a
        participant walked away from mid-round (see
        ``services.trivia.session.force_reveal_round``). Mirrors
        ``GameSessionQuerySet.stalled()``.
        """
        from urbanlens.dashboard.models.trivia.model import TriviaSessionStatus

        return self.filter(status=TriviaSessionStatus.ACTIVE, rounds__revealed_at__isnull=True, rounds__created__lte=cutoff).distinct()


class TriviaSessionManager(abstract.DashboardManager.from_queryset(TriviaSessionQuerySet)):
    """Manager for TriviaSession."""


class TriviaSessionParticipantQuerySet(abstract.DashboardQuerySet["TriviaSessionParticipant"]):
    """QuerySet for TriviaSessionParticipant."""

    def joined(self) -> TriviaSessionParticipantQuerySet:
        """Restrict to participants who have actually accepted (not just invited)."""
        from urbanlens.dashboard.models.trivia.model import TriviaSessionParticipantStatus

        return self.filter(status=TriviaSessionParticipantStatus.JOINED)


class TriviaSessionParticipantManager(abstract.DashboardManager.from_queryset(TriviaSessionParticipantQuerySet)):
    """Manager for TriviaSessionParticipant."""


class TriviaRoundQuerySet(abstract.DashboardQuerySet["TriviaRound"]):
    """QuerySet for TriviaRound."""

    def for_session(self, session: TriviaSession) -> TriviaRoundQuerySet:
        """Every round of ``session``, in play order."""
        return self.filter(session=session).order_by("sequence_index")


class TriviaRoundManager(abstract.DashboardManager.from_queryset(TriviaRoundQuerySet)):
    """Manager for TriviaRound."""


class TriviaAnswerQuerySet(abstract.DashboardQuerySet["TriviaAnswer"]):
    """QuerySet for TriviaAnswer."""

    def for_round(self, round_: TriviaRound) -> TriviaAnswerQuerySet:
        """Every answer submitted for ``round_``."""
        return self.filter(round=round_)


class TriviaAnswerManager(abstract.DashboardManager.from_queryset(TriviaAnswerQuerySet)):
    """Manager for TriviaAnswer."""


class TriviaSessionChatMessageQuerySet(abstract.DashboardQuerySet["TriviaSessionChatMessage"]):
    """QuerySet for TriviaSessionChatMessage."""

    def for_session(self, session: TriviaSession) -> TriviaSessionChatMessageQuerySet:
        """Every chat message in ``session``, oldest first."""
        return self.filter(session=session).order_by("created")


class TriviaSessionChatMessageManager(abstract.DashboardManager.from_queryset(TriviaSessionChatMessageQuerySet)):
    """Manager for TriviaSessionChatMessage."""
