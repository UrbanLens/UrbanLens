# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Count, Prefetch

# Django Imports
# App Imports
from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class TripQuerySet(abstract.QuerySet):
    """Custom queryset for Trip models."""

    def for_list_page(self, profile: Profile) -> TripQuerySet:
        """Return trips for the list page with counts and member prefetch.

        Args:
            profile: The viewer's profile; only their trips are included.

        Returns:
            Annotated queryset ordered by most recently updated.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        return (
            self.filter(profiles=profile)
            .select_related("creator__user")
            .annotate(
                activity_count=Count("activities", distinct=True),
                member_count=Count("memberships", distinct=True),
                comment_count=Count("comments", distinct=True),
            )
            .prefetch_related(
                Prefetch(
                    "memberships",
                    queryset=TripMembership.objects.select_related("profile__user").order_by(
                        "-is_organizer",
                        "created",
                    ),
                ),
            )
            .order_by("-updated")
        )


class TripManager(abstract.Manager.from_queryset(TripQuerySet)):
    """Custom query manager for Trip models."""
