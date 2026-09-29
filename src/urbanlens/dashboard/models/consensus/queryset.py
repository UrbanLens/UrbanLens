"""QuerySets/Managers for Consensus models (only scope/fetch rows; math lives in services.consensus)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.consensus.model import (  # noqa: F401 - mypy needs these; ruff does not
        ConsensusAnswer,
        ConsensusProfile,
        ConsensusRound,
        ConsensusRoundPhoto,
        ConsensusSession,
        ConsensusSessionChatMessage,
        ConsensusSessionParticipant,
        ConsensusTentativeAnswer,
        ConsensusVote,
    )
    from urbanlens.dashboard.models.profile.model import Profile


class ConsensusProfileQuerySet(abstract.DashboardQuerySet["ConsensusProfile"]):
    """QuerySet for ConsensusProfile."""


_ConsensusProfileManagerBase = abstract.DashboardManager.from_queryset(ConsensusProfileQuerySet)


class ConsensusProfileManager(_ConsensusProfileManagerBase["ConsensusProfile"]):
    """Manager for ConsensusProfile."""

    def get_or_create_for(self, profile: Profile) -> ConsensusProfile:
        """Return ``profile``'s Consensus stats row, creating it (at zero points/neutral trust) if missing."""
        consensus_profile, _ = self.get_or_create(profile=profile)
        return consensus_profile


class ConsensusSessionQuerySet(abstract.DashboardQuerySet["ConsensusSession"]):
    """QuerySet for ConsensusSession."""

    def answer_stalled(self, *, cutoff: datetime) -> Self:
        """ACTIVE sessions past ``cutoff`` with answers still pending."""
        from urbanlens.dashboard.models.consensus.model import ConsensusRoundResolution, ConsensusSessionStatus

        return self.filter(
            status=ConsensusSessionStatus.ACTIVE,
            rounds__resolution=ConsensusRoundResolution.PENDING,
            rounds__created__lte=cutoff,
        ).distinct()

    def vote_stalled(self, *, cutoff: datetime) -> Self:
        """ACTIVE sessions past ``cutoff`` with votes still open."""
        from urbanlens.dashboard.models.consensus.model import ConsensusRoundResolution, ConsensusSessionStatus

        return self.filter(
            status=ConsensusSessionStatus.ACTIVE,
            rounds__resolution=ConsensusRoundResolution.VOTE_OPEN,
            rounds__vote_opened_at__lte=cutoff,
        ).distinct()


_ConsensusSessionManagerBase = abstract.DashboardManager.from_queryset(ConsensusSessionQuerySet)


class ConsensusSessionManager(_ConsensusSessionManagerBase):
    """Manager for ConsensusSession."""


class ConsensusSessionParticipantQuerySet(abstract.DashboardQuerySet["ConsensusSessionParticipant"]):
    """QuerySet for ConsensusSessionParticipant."""

    def joined(self) -> Self:
        """Restrict to participants who accepted."""
        from urbanlens.dashboard.models.consensus.model import ConsensusSessionParticipantStatus

        return self.filter(status=ConsensusSessionParticipantStatus.JOINED)

    def active(self) -> Self:
        """Participants who still have access to their session. Every status qualifies: none marks a departure."""
        return self.all()


_ConsensusSessionParticipantManagerBase = abstract.DashboardManager.from_queryset(ConsensusSessionParticipantQuerySet)


class ConsensusSessionParticipantManager(_ConsensusSessionParticipantManagerBase):
    """Manager for ConsensusSessionParticipant."""


class ConsensusRoundQuerySet(abstract.DashboardQuerySet["ConsensusRound"]):
    """QuerySet for ConsensusRound."""

    def for_session(self, session: ConsensusSession) -> Self:
        """Every round of ``session``, in play order."""
        return self.filter(session=session).order_by("sequence_index")


_ConsensusRoundManagerBase = abstract.DashboardManager.from_queryset(ConsensusRoundQuerySet)


class ConsensusRoundManager(_ConsensusRoundManagerBase):
    """Manager for ConsensusRound."""


class ConsensusAnswerQuerySet(abstract.DashboardQuerySet["ConsensusAnswer"]):
    """QuerySet for ConsensusAnswer."""

    def for_round(self, round_: ConsensusRound) -> Self:
        """Every answer submitted for ``round_``."""
        return self.filter(round=round_)


_ConsensusAnswerManagerBase = abstract.DashboardManager.from_queryset(ConsensusAnswerQuerySet)


class ConsensusAnswerManager(_ConsensusAnswerManagerBase):
    """Manager for ConsensusAnswer."""


class ConsensusVoteQuerySet(abstract.DashboardQuerySet["ConsensusVote"]):
    """QuerySet for ConsensusVote."""

    def for_round(self, round_: ConsensusRound) -> Self:
        """Every vote cast for ``round_``."""
        return self.filter(round=round_)


_ConsensusVoteManagerBase = abstract.DashboardManager.from_queryset(ConsensusVoteQuerySet)


class ConsensusVoteManager(_ConsensusVoteManagerBase):
    """Manager for ConsensusVote."""


class ConsensusTentativeAnswerQuerySet(abstract.DashboardQuerySet["ConsensusTentativeAnswer"]):
    """QuerySet for ConsensusTentativeAnswer."""


_ConsensusTentativeAnswerManagerBase = abstract.DashboardManager.from_queryset(ConsensusTentativeAnswerQuerySet)


class ConsensusTentativeAnswerManager(_ConsensusTentativeAnswerManagerBase):
    """Manager for ConsensusTentativeAnswer."""


class ConsensusRoundPhotoQuerySet(abstract.DashboardQuerySet["ConsensusRoundPhoto"]):
    """QuerySet for ConsensusRoundPhoto."""


_ConsensusRoundPhotoManagerBase = abstract.DashboardManager.from_queryset(ConsensusRoundPhotoQuerySet)


class ConsensusRoundPhotoManager(_ConsensusRoundPhotoManagerBase):
    """Manager for ConsensusRoundPhoto."""


class ConsensusSessionChatMessageQuerySet(abstract.DashboardQuerySet["ConsensusSessionChatMessage"]):
    """QuerySet for ConsensusSessionChatMessage."""

    def for_session(self, session: ConsensusSession) -> Self:
        """Every chat message in ``session``, oldest first."""
        return self.filter(session=session).order_by("created")


_ConsensusSessionChatMessageManagerBase = abstract.DashboardManager.from_queryset(ConsensusSessionChatMessageQuerySet)


class ConsensusSessionChatMessageManager(_ConsensusSessionChatMessageManagerBase):
    """Manager for ConsensusSessionChatMessage."""
