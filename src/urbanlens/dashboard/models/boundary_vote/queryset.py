"""QuerySet/Manager for BoundaryVote.

The recency-weighted tallying itself lives in ``services.geo.boundary_voting`` -
these helpers only scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile


class BoundaryVoteQuerySet(abstract.DashboardQuerySet["BoundaryVote"]):
    """QuerySet for BoundaryVote, scoped by place/profile."""

    def for_place(self, place: Place | None) -> BoundaryVoteQuerySet:
        """Restrict to votes cast on ``place``'s official boundary."""
        if place is None:
            return self.none()
        return self.filter(place=place)

    def my_vote(self, place: Place | None, profile: Profile | None) -> BoundaryVote | None:
        """Return ``profile``'s own vote row for ``place``, if any."""
        if profile is None or place is None:
            return None
        return self.for_place(place).filter(profile=profile).first()


class BoundaryVoteManager(abstract.DashboardManager.from_queryset(BoundaryVoteQuerySet)):
    """Manager for BoundaryVote."""
