"""BoundaryVote - one profile's pick of a location's most accurate official boundary.

When more than one external provider has geometry for a place (REData's
county parcel vs. Overpass's OpenStreetMap perimeter), the community picks
which one should be the location's *official* property boundary - the one
used for matching pins to wikis. Votes are recency-weighted (see
``services.boundary_voting``), so a newer vote outweighs an equally-split
older one and the consensus can drift as the underlying data improves.

Only externally-sourced candidate ``Boundary`` rows are votable - a
user-drawn shape can never become the official matching boundary, which is
the whole point of restricting the choice to a vote between providers.

One row per (location, profile): changing your vote updates the row's
``boundary`` and its ``updated`` timestamp, which is what the recency
weighting reads - re-affirming a choice refreshes its weight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.boundary_vote.queryset import BoundaryVoteManager


class BoundaryVote(abstract.DashboardModel):
    """A single profile's vote for one candidate official boundary of a location.

    Attributes:
        location: The place whose official boundary is under vote. Candidate
            boundaries are location-scoped (see ``Boundary``'s source-candidate
            rows), so the vote is too - a wiki reaches it through its location.
        boundary: The chosen candidate row. Must be one of the location's own
            source-candidate boundaries - enforced at the endpoint, since a
            CHECK constraint can't join across tables.
        profile: Who cast it.
    """

    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="boundary_votes",
    )
    boundary = ForeignKey(
        "dashboard.Boundary",
        on_delete=CASCADE,
        related_name="votes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="boundary_votes",
    )

    if TYPE_CHECKING:
        location_id: int
        boundary_id: int
        profile_id: int

    objects = BoundaryVoteManager()

    def __str__(self) -> str:
        return f"Boundary vote for boundary {self.boundary_id} on location {self.location_id} by profile {self.profile_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_boundary_votes"
        indexes = [
            Index(fields=["location"], name="idxdb_bv_location"),
        ]
        constraints = [
            UniqueConstraint(fields=["location", "profile"], name="db_boundary_vote_unique"),
        ]
