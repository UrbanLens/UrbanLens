# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import Count, F, Max, Prefetch, Q
from django.utils import timezone

# Django Imports
# App Imports
from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    import datetime

    from django.db.models import QuerySet

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

        # Filter to a pk subquery rather than `.filter(profiles=profile)` directly:
        # the latter joins through the same `memberships` relation the
        # `member_count` annotation below also joins through, and Django reuses
        # that join - so the annotation's COUNT would silently inherit this
        # filter's `profile_id = viewer` clause and always come out as 1.
        trip_ids = self.filter(profiles=profile).values_list("pk", flat=True)
        qs = (
            self.filter(pk__in=trip_ids)
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

    def upcoming(self, profile: Profile) -> TripQuerySet:
        """Return the viewer's upcoming (or still-planning, undated) trips.

        A trip counts as upcoming if it has a future/today start date, or has
        no start date at all but at least one activity scheduled today or later.

        Args:
            profile: The viewer's profile; only their trips are included.

        Returns:
            Matching trips, unordered (callers apply their own ordering/limit).
        """
        today = timezone.now().date()
        return self.filter(profiles=profile).filter(Q(start_date__gte=today) | Q(start_date__isnull=True, activities__scheduled_at__date__gte=today)).distinct()

    def recently_updated(self, profile: Profile, limit: int = 5) -> TripQuerySet:
        """Return the viewer's trips ordered by most recently updated, for the overview page.

        Args:
            profile: The viewer's profile; only their trips are included.
            limit: Maximum number of trips to return.

        Returns:
            Trips ordered by `updated` descending, limited to `limit`.
        """
        return self.filter(profiles=profile).select_related("creator__user").order_by("-updated")[:limit]

    def recently_active_past(self, profile: Profile, since: datetime.datetime, limit: int = 6) -> list[Trip]:
        """Return the viewer's past trips that have had a comment posted since ``since``.

        Args:
            profile: The viewer's profile; only their trips are included.
            since: Cutoff datetime - only trips with a comment created at or
                after this time qualify.
            limit: Maximum number of trips to return.

        Returns:
            Trips whose `Trip.timeline_status` is `"past"`, with at least one
            comment posted since `since`, most recently commented first.
            `timeline_status` depends on activity dates that aren't directly
            queryable, so candidates are DB-filtered on comment recency first,
            then narrowed to "past" in Python.
        """
        from urbanlens.dashboard.models.trips.model import TripComment

        trip_ids = TripComment.objects.filter(trip__profiles=profile, created__gte=since).values_list("trip_id", flat=True).distinct()
        last_comment_by_trip: dict[int, datetime.datetime] = dict(
            TripComment.objects.filter(trip_id__in=trip_ids, created__gte=since).values("trip_id").annotate(last=Max("created")).values_list("trip_id", "last"),
        )
        candidates: TripQuerySet = self.filter(pk__in=trip_ids).select_related("creator__user").prefetch_related("memberships", "activities")
        past = [trip for trip in candidates if trip.timeline_status == "past"]
        past.sort(key=lambda trip: last_comment_by_trip[trip.pk], reverse=True)
        return past[:limit]

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


class TripMembershipQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for TripMembership models."""

    def for_trip_and_profile(self, trip: Trip, profile: Profile) -> TripMembershipQuerySet:
        """The membership row for a specific trip+profile pair (the unique_together key).

        Args:
            trip: The trip.
            profile: The member's profile.

        Returns:
            A queryset matching at most one row (unique_together on trip+profile).
        """
        return self.filter(trip=trip, profile=profile)

    def trip_ids_for(self, profile: Profile) -> QuerySet[Any, Any]:
        """IDs of every trip this profile has a membership row for.

        Args:
            profile: The profile (or a raw profile id) to look up.

        Returns:
            A flat ``values_list`` queryset of trip ids.
        """
        return self.filter(profile=profile).values_list("trip_id", flat=True)

    def joined(self, trip: Trip) -> TripMembershipQuerySet:
        """Members who have actually joined (not just invited) a trip.

        Args:
            trip: The trip.

        Returns:
            Matching membership rows.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        return self.filter(trip=trip, status=TripMembership.STATUS_JOINED)

    def rsvp_yes(self, trip: Trip) -> TripMembershipQuerySet:
        """Members who RSVP'd yes to a trip.

        Args:
            trip: The trip.

        Returns:
            Matching membership rows.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        return self.filter(trip=trip, rsvp=TripMembership.RSVP_YES)


class TripMembershipManager(abstract.DashboardManager.from_queryset(TripMembershipQuerySet)):
    """Custom query manager for TripMembership models."""


class TripCommentQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for TripComment models."""

    def by_author(self, profile: Profile) -> TripCommentQuerySet:
        """Comments a profile has left across any of their trips, most recent first.

        Args:
            profile: The commenting profile.

        Returns:
            Matching comments with their trip preloaded, most recently created first.
        """
        return self.filter(author=profile).select_related("trip").order_by("-created")


class TripCommentManager(abstract.DashboardManager.from_queryset(TripCommentQuerySet)):
    """Custom query manager for TripComment models."""
