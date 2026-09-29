"""QuerySets/Managers for Trivia models.

Glicko-2 rating math lives in ``services.games.glicko2`` (reused
directly); eligibility, question selection, and vote scoring live in
``services.trivia.eligibility``/``selection``/``voting``. These classes only
scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trivia.model import (  # noqa: F401 - mypy needs these; ruff does not
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

    def approved(self) -> TriviaQuestionQuerySet:
        """Restrict to questions that passed moderation and are eligible for rotation."""
        from urbanlens.dashboard.models.trivia.model import TriviaQuestionStatus

        return self.filter(status=TriviaQuestionStatus.APPROVED)


_TriviaQuestionManagerBase = abstract.DashboardManager.from_queryset(TriviaQuestionQuerySet)


class TriviaQuestionManager(_TriviaQuestionManagerBase):
    """Manager for TriviaQuestion."""


class TriviaQuestionVoteQuerySet(abstract.DashboardQuerySet["TriviaQuestionVote"]):
    """QuerySet for TriviaQuestionVote."""

    def for_question(self, question: TriviaQuestion) -> TriviaQuestionVoteQuerySet:
        """Every vote recorded for ``question``."""
        return self.filter(question=question)


_TriviaQuestionVoteManagerBase = abstract.DashboardManager.from_queryset(TriviaQuestionVoteQuerySet)


class TriviaQuestionVoteManager(_TriviaQuestionVoteManagerBase):
    """Manager for TriviaQuestionVote."""


class PlayerTriviaRatingQuerySet(abstract.DashboardQuerySet["PlayerTriviaRating"]):
    """QuerySet for PlayerTriviaRating."""


_PlayerTriviaRatingManagerBase = abstract.DashboardManager.from_queryset(PlayerTriviaRatingQuerySet)


class PlayerTriviaRatingManager(_PlayerTriviaRatingManagerBase["PlayerTriviaRating"]):
    """Manager for PlayerTriviaRating."""

    def get_or_create_for(self, profile: Profile) -> PlayerTriviaRating:
        """Return ``profile``'s Trivia rating row, creating it at the default rating if missing."""
        rating, _ = self.get_or_create(profile=profile)
        return rating


class TriviaQuestionRatingQuerySet(abstract.DashboardQuerySet["TriviaQuestionRating"]):
    """QuerySet for TriviaQuestionRating."""


_TriviaQuestionRatingManagerBase = abstract.DashboardManager.from_queryset(TriviaQuestionRatingQuerySet)


class TriviaQuestionRatingManager(_TriviaQuestionRatingManagerBase["TriviaQuestionRating"]):
    """Manager for TriviaQuestionRating."""

    def get_or_create_for(self, question: TriviaQuestion) -> TriviaQuestionRating:
        """Return ``question``'s difficulty rating row, creating it at the default rating if missing."""
        rating, _ = self.get_or_create(question=question)
        return rating


class TriviaSessionQuerySet(abstract.DashboardQuerySet["TriviaSession"]):
    """QuerySet for TriviaSession."""

    def stalled(self, *, cutoff: datetime) -> TriviaSessionQuerySet:
        """ACTIVE sessions whose current round was created before ``cutoff`` and still isn't revealed.
        ``get_or_create_round`` never creates a session's next round until its prior one is fully revealed, so at most one round per session can ever match "unrevealed" at a time - this is always that session's current round.
        """
        from urbanlens.dashboard.models.trivia.model import TriviaSessionStatus

        return self.filter(status=TriviaSessionStatus.ACTIVE, rounds__revealed_at__isnull=True, rounds__created__lte=cutoff).distinct()


_TriviaSessionManagerBase = abstract.DashboardManager.from_queryset(TriviaSessionQuerySet)


class TriviaSessionManager(_TriviaSessionManagerBase):
    """Manager for TriviaSession."""


class TriviaSessionParticipantQuerySet(abstract.DashboardQuerySet["TriviaSessionParticipant"]):
    """QuerySet for TriviaSessionParticipant."""

    def joined(self) -> TriviaSessionParticipantQuerySet:
        """Restrict to participants who have actually accepted (not just invited)."""
        from urbanlens.dashboard.models.trivia.model import TriviaSessionParticipantStatus

        return self.filter(status=TriviaSessionParticipantStatus.JOINED)

    def active(self) -> TriviaSessionParticipantQuerySet:
        """Participants who still have access to their session: invited or joined, not departed."""
        from urbanlens.dashboard.models.trivia.model import TriviaSessionParticipantStatus

        return self.exclude(status=TriviaSessionParticipantStatus.LEFT)


_TriviaSessionParticipantManagerBase = abstract.DashboardManager.from_queryset(TriviaSessionParticipantQuerySet)


class TriviaSessionParticipantManager(_TriviaSessionParticipantManagerBase):
    """Manager for TriviaSessionParticipant."""


class TriviaRoundQuerySet(abstract.DashboardQuerySet["TriviaRound"]):
    """QuerySet for TriviaRound."""

    def for_session(self, session: TriviaSession) -> TriviaRoundQuerySet:
        """Every round of ``session``, in play order."""
        return self.filter(session=session).order_by("sequence_index")


_TriviaRoundManagerBase = abstract.DashboardManager.from_queryset(TriviaRoundQuerySet)


class TriviaRoundManager(_TriviaRoundManagerBase):
    """Manager for TriviaRound."""


class TriviaAnswerQuerySet(abstract.DashboardQuerySet["TriviaAnswer"]):
    """QuerySet for TriviaAnswer."""

    def for_round(self, round_: TriviaRound) -> TriviaAnswerQuerySet:
        """Every answer submitted for ``round_``."""
        return self.filter(round=round_)


_TriviaAnswerManagerBase = abstract.DashboardManager.from_queryset(TriviaAnswerQuerySet)


class TriviaAnswerManager(_TriviaAnswerManagerBase):
    """Manager for TriviaAnswer."""


class TriviaSessionChatMessageQuerySet(abstract.DashboardQuerySet["TriviaSessionChatMessage"]):
    """QuerySet for TriviaSessionChatMessage."""

    def for_session(self, session: TriviaSession) -> TriviaSessionChatMessageQuerySet:
        """Every chat message in ``session``, oldest first."""
        return self.filter(session=session).order_by("created")


_TriviaSessionChatMessageManagerBase = abstract.DashboardManager.from_queryset(TriviaSessionChatMessageQuerySet)


class TriviaSessionChatMessageManager(_TriviaSessionChatMessageManagerBase):
    """Manager for TriviaSessionChatMessage."""
