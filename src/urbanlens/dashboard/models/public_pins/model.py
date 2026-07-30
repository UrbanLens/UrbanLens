"""Public-pin voting - the community process that can make a Location public.

A tiny, highly selective set of locations can be voted "public" by the users
who have them pinned. Public locations are suggested to every account
(opt-out), which gives new users a populated map without exposing anything
vulnerable. Eligibility is computed entirely server-side by
``services.public_pins`` on a schedule - users never see the rule engine,
only the vote buttons when a place qualifies.

Votes are anonymous in the UI: only the voter ever sees their own choice,
and no running tallies are shown before an outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, BooleanField, DateTimeField, ForeignKey, Index, OneToOneField, UniqueConstraint
from django.db.models.fields import CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.public_pins.queryset import PublicPinCandidateManager, PublicPinVoteManager


class PublicPinCandidateStatus(abstract.TextChoices):
    """Lifecycle of a public-pin candidate.

    OPEN and SUSPENDED flip back and forth as eligibility comes and goes;
    PASSED and REJECTED are terminal.
    """

    OPEN = "open", "Open"
    SUSPENDED = "suspended", "Suspended"
    PASSED = "passed", "Passed"
    REJECTED = "rejected", "Rejected"


class PublicPinCandidate(abstract.DashboardModel):
    """One Location's public-pin vote, created only by the eligibility engine.

    Attributes:
        location: The shared Location under vote. One candidate per location,
            ever - a REJECTED candidate permanently blocks re-nomination.
        status: See :class:`PublicPinCandidateStatus`.
        opened_at: When the vote first opened. Set once and never reset by
            suspension: the minimum-open-time clock measures total time since
            opening, so a flapping criterion can't filibuster the vote.
        decided_at: When the vote reached PASSED or REJECTED.
    """

    status = CharField(max_length=20, choices=PublicPinCandidateStatus.choices, default=PublicPinCandidateStatus.OPEN)
    opened_at = DateTimeField()
    decided_at = DateTimeField(null=True, blank=True)

    location = OneToOneField(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="public_candidate",
    )

    if TYPE_CHECKING:
        location_id: int

    objects = PublicPinCandidateManager()

    @property
    def is_open(self) -> bool:
        """Whether votes may currently be cast or changed."""
        return self.status == PublicPinCandidateStatus.OPEN

    def __str__(self) -> str:
        return f"Public-pin candidate ({self.status}) for location {self.location_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_public_pin_candidates"
        indexes = [
            Index(fields=["status"], name="idxdb_ppc_status"),
        ]


class PublicPinVote(abstract.DashboardModel):
    """A single profile's yes/no vote on making a location public.

    Withdrawing a vote deletes the row (mirroring WikiStatVote), so tallies
    never include "no opinion" entries.

    Attributes:
        candidate: The vote this ballot belongs to.
        profile: Who cast it. Only profiles with a root pin at the candidate's
            location may vote - enforced in ``services.public_pins``.
        make_public: True for "make it public", False for "keep it private".
    """

    make_public = BooleanField()

    candidate = ForeignKey(
        "dashboard.PublicPinCandidate",
        on_delete=CASCADE,
        related_name="votes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="public_pin_votes",
    )

    if TYPE_CHECKING:
        candidate_id: int
        profile_id: int

    objects = PublicPinVoteManager()

    def __str__(self) -> str:
        return f"{'public' if self.make_public else 'private'} vote on candidate {self.candidate_id} by profile {self.profile_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_public_pin_votes"
        constraints = [
            UniqueConstraint(fields=["candidate", "profile"], name="db_public_pin_vote_unique"),
        ]
