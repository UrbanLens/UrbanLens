# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Count, F, Max, Prefetch, Q
from django.utils import timezone

# Django Imports
# App Imports
from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip

logger = logging.getLogger(__name__)

#: Valid values for the ``sort`` argument of :meth:`TripQuerySet.for_list_page`, mapped
#: to the model field each sorts on.
TRIP_LIST_SORT_FIELDS: dict[str, str] = {
    "start_date": "start_date",
    "updated": "updated",
}


class TripQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for Trip models."""

    def for_list_page(self, profile: Profile, sort: str = "updated", direction: str = "desc") -> TripQuerySet | list[Trip]:
        """Return trips for the list page with counts and member prefetch.

        Args:
            profile: The viewer's profile; only their trips are included.
            sort: Which field to order by - one of the keys in ``TRIP_LIST_SORT_FIELDS``
                (``"start_date"`` or ``"updated"``). Falls back to ``"updated"`` if unrecognized.
            direction: ``"asc"`` or ``"desc"``. Falls back to ``"desc"`` if unrecognized.

        Returns:
            Annotated queryset ordered per ``sort``/``direction``. Trips with no ``start_date``
            always sort to the end regardless of direction when sorting by ``start_date``.
            For ``sort="start_date"``/``direction="asc"`` ("soonest first"), the result is a
            plain list grouped as: upcoming/active trips soonest first, then undated
            (planning) trips, then past trips most-recent first.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        field = TRIP_LIST_SORT_FIELDS.get(sort, "updated")
        ascending = direction == "asc"
        if field == "start_date":
            order = F(field).asc(nulls_last=True) if ascending else F(field).desc(nulls_last=True)
        else:
            order = F(field).asc() if ascending else F(field).desc()

        qs = (
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
            .order_by(order)
        )

        if field == "start_date" and ascending:
            return self._soonest_first(qs)
        return qs

    @staticmethod
    def _soonest_first(qs: TripQuerySet) -> list[Trip]:
        """Reorder a ``start_date``-sorted queryset so past trips sink to the bottom.

        Upcoming/active trips sort soonest first, undated (planning) trips sort next,
        and past trips sort most-recent first - rather than the plain chronological
        ordering (which would otherwise interleave "soonest" with the most stale past
        trips as equally "soon" once their dates have passed).

        Args:
            qs: A queryset already ordered by ``start_date`` ascending (nulls last).

        Returns:
            A plain list of trips in the grouped order described above.
        """
        today = timezone.now().date()

        def bucket_key(trip: Trip) -> tuple[int, int]:
            start = trip.start_date
            if start is None:
                return (1, 0)
            if start >= today:
                return (0, start.toordinal())
            return (2, -start.toordinal())

        return sorted(qs, key=bucket_key)

    def recently_updated(self, profile: Profile, limit: int = 5) -> TripQuerySet:
        """Return the viewer's trips ordered by most recently updated, for the overview page.

        Args:
            profile: The viewer's profile; only their trips are included.
            limit: Maximum number of trips to return.

        Returns:
            Trips ordered by `updated` descending, limited to `limit`.
        """
        return self.filter(profiles=profile).select_related("creator__user").order_by("-updated")[:limit]

    def recently_viewed(self, profile: Profile, limit: int = 5) -> TripQuerySet:
        """Return the viewer's trips ordered by when they personally last viewed each one.

        Args:
            profile: The viewer's profile; only trips they've opened before are included.
            limit: Maximum number of trips to return.

        Returns:
            Trips ordered by the viewer's own `TripMembership.last_viewed_at` descending,
            excluding trips they belong to but have never opened.
        """
        return (
            self.filter(profiles=profile)
            .annotate(viewer_last_viewed_at=Max("memberships__last_viewed_at", filter=Q(memberships__profile=profile)))
            .filter(viewer_last_viewed_at__isnull=False)
            .select_related("creator__user")
            .order_by("-viewer_last_viewed_at")[:limit]
        )


class TripManager(abstract.DashboardManager.from_queryset(TripQuerySet)):
    """Custom query manager for Trip models."""
