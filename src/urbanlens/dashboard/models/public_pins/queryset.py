"""QuerySets/Managers for public-pin candidates and votes, including tallying."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from django.db.models import Count, Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinVote


@dataclass(slots=True, frozen=True)
class PublicVoteTally:
    """Aggregate ballot counts for one candidate - never shown to users pre-outcome."""

    yes: int
    no: int

    @property
    def total(self) -> int:
        return self.yes + self.no

    @property
    def yes_share(self) -> float:
        """Fraction of ballots voting "make it public" (0.0 with no ballots)."""
        return self.yes / self.total if self.total else 0.0

    @property
    def no_share(self) -> float:
        """Fraction of ballots voting "keep it private" (0.0 with no ballots)."""
        return self.no / self.total if self.total else 0.0


class PublicPinCandidateQuerySet(abstract.DashboardQuerySet["PublicPinCandidate"]):
    """QuerySet for PublicPinCandidate, scoped by lifecycle status."""

    def with_status(self, status: str) -> Self:
        """Restrict to candidates in the given lifecycle status."""
        return self.filter(status=status)

    def passed(self) -> Self:
        """Candidates whose location is now public."""
        from urbanlens.dashboard.models.public_pins.model import PublicPinCandidateStatus

        return self.with_status(PublicPinCandidateStatus.PASSED)

    def active(self) -> Self:
        """Candidates still in play (OPEN or SUSPENDED)."""
        from urbanlens.dashboard.models.public_pins.model import PublicPinCandidateStatus

        return self.filter(status__in=[PublicPinCandidateStatus.OPEN, PublicPinCandidateStatus.SUSPENDED])


class PublicPinCandidateManager(abstract.DashboardManager.from_queryset(PublicPinCandidateQuerySet)):
    """Manager for PublicPinCandidate."""


class PublicPinVoteQuerySet(abstract.DashboardQuerySet["PublicPinVote"]):
    """QuerySet for PublicPinVote."""

    def tally(self, candidate: PublicPinCandidate) -> PublicVoteTally:
        """Count yes/no ballots for ``candidate`` in one query."""
        agg = self.filter(candidate=candidate).aggregate(
            yes=Count("id", filter=Q(make_public=True)),
            no=Count("id", filter=Q(make_public=False)),
        )
        return PublicVoteTally(yes=agg["yes"] or 0, no=agg["no"] or 0)


class PublicPinVoteManager(abstract.DashboardManager.from_queryset(PublicPinVoteQuerySet)):
    """Manager for PublicPinVote."""
