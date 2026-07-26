"""QuerySets/Managers for Consensus models.

Points/leveling math lives in ``services.consensus.points``; trust scoring in
``services.consensus.trust``; eligibility and field-kind selection in
``services.consensus.eligibility``/``selection``. These classes only scope
and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.consensus.model import ConsensusProfile, ConsensusRound, ConsensusSession
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


class ConsensusProfileQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusProfile."""


class ConsensusProfileManager(abstract.DashboardManager.from_queryset(ConsensusProfileQuerySet)):
    """Manager for ConsensusProfile."""

    def get_or_create_for(self, profile: Profile) -> ConsensusProfile:
        """Return ``profile``'s Consensus stats row, creating it (at zero points/neutral trust) if missing."""
        consensus_profile, _ = self.get_or_create(profile=profile)
        return consensus_profile


class ConsensusSessionQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusSession."""

    def active(self) -> Self:
        """Restrict to sessions still in progress (lobby or active)."""
        from urbanlens.dashboard.models.consensus.model import ConsensusSessionStatus

        return self.filter(status__in=[ConsensusSessionStatus.LOBBY, ConsensusSessionStatus.ACTIVE])

    def for_profile(self, profile: Profile) -> Self:
        """Restrict to sessions ``profile`` is (or was) a participant in, any status."""
        return self.filter(participants__profile=profile).distinct()

    def answer_stalled(self, *, cutoff: datetime) -> Self:
        """ACTIVE sessions whose current round is still collecting answers past ``cutoff``.

        Used by the stall-sweep Celery task (``tasks.sweep_stalled_consensus_sessions``)
        to find sessions a participant walked away from mid-round - mirrors
        ``GameSessionQuerySet.stalled``.
        """
        from urbanlens.dashboard.models.consensus.model import ConsensusRoundResolution, ConsensusSessionStatus

        return self.filter(
            status=ConsensusSessionStatus.ACTIVE,
            rounds__resolution=ConsensusRoundResolution.PENDING,
            rounds__created__lte=cutoff,
        ).distinct()

    def vote_stalled(self, *, cutoff: datetime) -> Self:
        """ACTIVE sessions whose current round has an open vote stuck past ``cutoff``."""
        from urbanlens.dashboard.models.consensus.model import ConsensusRoundResolution, ConsensusSessionStatus

        return self.filter(
            status=ConsensusSessionStatus.ACTIVE,
            rounds__resolution=ConsensusRoundResolution.VOTE_OPEN,
            rounds__vote_opened_at__lte=cutoff,
        ).distinct()


class ConsensusSessionManager(abstract.DashboardManager.from_queryset(ConsensusSessionQuerySet)):
    """Manager for ConsensusSession."""


class ConsensusSessionParticipantQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusSessionParticipant."""

    def joined(self) -> Self:
        """Restrict to participants who have actually accepted (not just invited)."""
        from urbanlens.dashboard.models.consensus.model import ConsensusSessionParticipantStatus

        return self.filter(status=ConsensusSessionParticipantStatus.JOINED)


class ConsensusSessionParticipantManager(abstract.DashboardManager.from_queryset(ConsensusSessionParticipantQuerySet)):
    """Manager for ConsensusSessionParticipant."""


class ConsensusRoundQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusRound."""

    def for_session(self, session: ConsensusSession) -> Self:
        """Every round of ``session``, in play order."""
        return self.filter(session=session).order_by("sequence_index")


class ConsensusRoundManager(abstract.DashboardManager.from_queryset(ConsensusRoundQuerySet)):
    """Manager for ConsensusRound."""


class ConsensusAnswerQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusAnswer."""

    def for_round(self, round_: ConsensusRound) -> Self:
        """Every answer submitted for ``round_``."""
        return self.filter(round=round_)


class ConsensusAnswerManager(abstract.DashboardManager.from_queryset(ConsensusAnswerQuerySet)):
    """Manager for ConsensusAnswer."""


class ConsensusVoteQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusVote."""

    def for_round(self, round_: ConsensusRound) -> Self:
        """Every vote cast for ``round_``."""
        return self.filter(round=round_)


class ConsensusVoteManager(abstract.DashboardManager.from_queryset(ConsensusVoteQuerySet)):
    """Manager for ConsensusVote."""


class ConsensusTentativeAnswerQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusTentativeAnswer."""

    def for_wiki(self, wiki: Wiki) -> Self:
        """Every tentative answer proposed for ``wiki``, any status."""
        return self.filter(wiki=wiki)

    def pending(self) -> Self:
        """Tentative answers not yet applied or dismissed - still building consensus."""
        from urbanlens.dashboard.models.consensus.model import ConsensusTentativeStatus

        return self.filter(status=ConsensusTentativeStatus.PENDING)


class ConsensusTentativeAnswerManager(abstract.DashboardManager.from_queryset(ConsensusTentativeAnswerQuerySet)):
    """Manager for ConsensusTentativeAnswer."""


class ConsensusRoundPhotoQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusRoundPhoto."""

    def for_round(self, round_: ConsensusRound) -> Self:
        """Every photo captured during ``round_``."""
        return self.filter(round=round_)


class ConsensusRoundPhotoManager(abstract.DashboardManager.from_queryset(ConsensusRoundPhotoQuerySet)):
    """Manager for ConsensusRoundPhoto."""


class ConsensusSessionChatMessageQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ConsensusSessionChatMessage."""

    def for_session(self, session: ConsensusSession) -> Self:
        """Every chat message in ``session``, oldest first."""
        return self.filter(session=session).order_by("created")


class ConsensusSessionChatMessageManager(abstract.DashboardManager.from_queryset(ConsensusSessionChatMessageQuerySet)):
    """Manager for ConsensusSessionChatMessage."""
