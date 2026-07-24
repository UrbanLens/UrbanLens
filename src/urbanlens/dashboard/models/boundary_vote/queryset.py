"""QuerySet/Manager for BoundaryVote.

The recency-weighted tallying itself lives in ``services.boundary_voting`` -
these helpers only scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


class BoundaryVoteQuerySet(abstract.DashboardQuerySet["BoundaryVote"]):
    """QuerySet for BoundaryVote, scoped by location/profile."""

    def for_location(self, location: Location) -> BoundaryVoteQuerySet:
        """Restrict to votes cast on ``location``'s official boundary."""
        return self.filter(location=location)

    def my_vote(self, location: Location, profile: Profile | None) -> BoundaryVote | None:
        """Return ``profile``'s own vote row for ``location``, if any."""
        if profile is None:
            return None
        return self.for_location(location).filter(profile=profile).first()


class BoundaryVoteManager(abstract.DashboardManager.from_queryset(BoundaryVoteQuerySet)):
    """Manager for BoundaryVote."""
