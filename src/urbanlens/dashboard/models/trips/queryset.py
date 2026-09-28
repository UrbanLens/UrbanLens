# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import Case, CharField, Count, DateField, Exists, F, IntegerField, Max, Min, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Greatest
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

#: ``Trip.timeline_status`` values, in the order the overview's stat tiles list them.
TIMELINE_STATUSES: tuple[str, ...] = ("planning", "upcoming", "active", "past")

#: Valid values for the ``sort`` argument of :meth:`TripQuerySet.for_list_page`, mapped
#: to the model field each sorts on.
TRIP_LIST_SORT_FIELDS: dict[str, str] = {
    "start_date": "start_date",
    "updated": "updated",
}


def _member_profiles() -> Prefetch:
    """Prefetch each trip's memberships with their profile+user loaded.
    The overview masks every listed trip's member identities (``controllers.trip._apply_trip_list_identity_masking``), which walks ``trip.memberships.all()``; without this that is two queries per trip.

    Returns:
        The ``memberships`` Prefetch to hand to ``prefetch_related``.
    """
    from urbanlens.dashboard.models.trips.model import TripMembership

    return Prefetch("memberships", queryset=TripMembership.objects.select_related("profile__user"))


class TripQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for Trip models."""

    def with_effective_dates(self) -> TripQuerySet:
        """Annotate ``_eff_start``/``_eff_end`` so the date properties don't query per row.
        ``Trip.effective_start_date``/``effective_end_date`` fall back to querying the trip's activities, and ``timeline_status``/``duration_days`` read both, so any page rendering a list of trips pays two activity queries per trip without this.

        Correlated subqueries rather than a join aggregate, so the annotations are plain columns: they filter in ``WHERE``, :meth:`timeline_counts` can count them, and they combine with other aggregates without fanning out.

        Returns:
            The queryset with both annotations applied.
        """
        if "_eff_start" in self.query.annotations:
            return self
        from urbanlens.dashboard.models.trips.model import TripActivity

        activities = TripActivity.objects.filter(trip_id=OuterRef("pk")).order_by().values("trip_id")
        first = activities.annotate(value=Min("scheduled_at")).values("value")[:1]
        last = activities.annotate(value=Greatest(Max("scheduled_at"), Max("scheduled_end"))).values("value")[:1]
        return (
            self.annotate(
                _first_activity_date=Cast(Subquery(first), output_field=DateField()),
                _last_activity_date=Cast(Subquery(last), output_field=DateField()),
            )
            .annotate(_eff_start=Coalesce("start_date", "_first_activity_date"))
            .annotate(_eff_end=Coalesce("end_date", "_last_activity_date", "_eff_start"))
        )

    def with_timeline_status(self) -> TripQuerySet:
        """Annotate ``timeline``, the SQL form of ``Trip.timeline_status``.

        Returns:
            The queryset with effective dates and ``timeline`` annotated.
        """
        today = timezone.now().date()
        return self.with_effective_dates().annotate(
            timeline=Case(
                When(_eff_start__isnull=True, then=Value("planning")),
                When(_eff_start__gt=today, then=Value("upcoming")),
                When(_eff_end__lt=today, then=Value("past")),
                default=Value("active"),
                output_field=CharField(),
            ),
        )

    def timeline_counts(self) -> dict[str, int]:
        """How many of these trips are in each timeline status, in one query.

        Returns:
            ``total`` plus one key per :data:`TIMELINE_STATUSES` entry.
        """
        counts = self.with_timeline_status().aggregate(total=Count("pk"), **{status: Count("pk", filter=Q(timeline=status)) for status in TIMELINE_STATUSES})
        return {key: value or 0 for key, value in counts.items()}

    def overlapping(self, start: datetime.date, end: datetime.date) -> TripQuerySet:
        """Trips whose effective date range meets ``[start, end]``; undated trips never do.

        Args:
            start: First day of the window.
            end: Last day of the window, inclusive.

        Returns:
            The matching trips, with effective dates annotated.
        """
        return self.with_effective_dates().filter(_eff_start__isnull=False, _eff_start__lte=end, _eff_end__gte=start)

    def for_list_page(self, profile: Profile, sort: str = "updated", direction: str = "desc") -> TripQuerySet:
        """Return trips for the list page with counts and member prefetch, ordered in SQL.

        Args:
            profile: The viewer's profile; only their trips are included.
            sort: Which field to order by - one of the keys in ``TRIP_LIST_SORT_FIELDS``
                (``"start_date"`` or ``"updated"``). Falls back to ``"updated"`` if unrecognized.
            direction: ``"asc"`` or ``"desc"``. Falls back to ``"desc"`` if unrecognized.

        Returns:
            Annotated queryset ordered per ``sort``/``direction``, with a primary-key tie-break so
            pages never repeat or drop a trip. Trips with no ``start_date`` sort to the end regardless
            of direction when sorting by ``start_date``. ``start_date`` ascending ("soonest first")
            groups upcoming/active trips soonest first, then undated (planning) trips, then past trips
            most-recent first.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        field = TRIP_LIST_SORT_FIELDS.get(sort, "updated")
        ascending = direction == "asc"
        if field == "start_date" and ascending:
            ordering = self._soonest_first_ordering()
        elif field == "start_date":
            ordering = (F(field).desc(nulls_last=True), "-pk")
        else:
            ordering = (F(field).asc(), "pk") if ascending else (F(field).desc(), "-pk")

        # Filter to a pk subquery rather than `.filter(profiles=profile)` directly: the latter joins
        # through the same `memberships` relation the `member_count` annotation below also joins
        # through, and Django reuses that join - so the annotation's COUNT would silently inherit
        # this filter's `profile_id = viewer` clause and always come out as 1.
        trip_ids = self.filter(profiles=profile).values_list("pk", flat=True)
        return (
            self.filter(pk__in=trip_ids)
            .select_related("creator__user")
            .annotate(
                activity_count=Count("activities", distinct=True),
                member_count=Count("memberships", distinct=True),
                comment_count=Count("comments", distinct=True),
                pin_count=Count("activities__pin", distinct=True, filter=Q(activities__pin__isnull=False)),
            )
            .with_effective_dates()
            .prefetch_related(
                Prefetch(
                    "memberships",
                    queryset=TripMembership.objects.select_related("profile__user").order_by(
                        "-is_organizer",
                        "created",
                    ),
                ),
            )
            .order_by(*ordering)
        )

    @staticmethod
    def _soonest_first_ordering() -> tuple:
        """``ORDER BY`` terms putting upcoming/active trips first (soonest first), then undated, then past (most recent first).

        Returns:
            Terms for ``order_by``.
        """
        today = timezone.now().date()
        bucket = Case(
            When(start_date__isnull=True, then=Value(1)),
            When(start_date__gte=today, then=Value(0)),
            default=Value(2),
            output_field=IntegerField(),
        )
        upcoming = Case(When(start_date__gte=today, then=F("start_date")))
        past = Case(When(start_date__lt=today, then=F("start_date")))
        return (bucket.asc(), upcoming.asc(nulls_last=True), past.desc(nulls_last=True), "pk")

    def search_for_member(self, profile: Profile, query: str = "", limit: int = 20) -> TripQuerySet:
        """The viewer's trips whose name contains *query*, most recently updated first, capped.

        Args:
            profile: The viewer's profile; only their trips are included.
            query: Case-insensitive name fragment; blank matches every trip.
            limit: The most to return.

        Returns:
            At most *limit* trips.
        """
        qs = self.filter(pk__in=self.filter(profiles=profile).values("pk"))
        if query:
            qs = qs.filter(name__icontains=query)
        return qs.order_by("-updated", "-pk")[:limit]

    def upcoming(self, profile: Profile) -> TripQuerySet:
        """Return the viewer's upcoming (or still-planning, undated) trips.
        A trip counts as upcoming if it has a future/today start date, or has no start date at all but at least one activity scheduled today or later.

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
        return self.filter(profiles=profile).select_related("creator__user").prefetch_related(_member_profiles()).with_effective_dates().order_by("-updated")[:limit]

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

        trip_ids = TripComment.objects.for_member(profile).filter(created__gte=since).values_list("trip_id", flat=True).distinct()
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
            .prefetch_related(_member_profiles())
            .with_effective_dates()
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

    def mentions_all_visible_to(self, profile: Profile) -> TripCommentQuerySet:
        """Drop comments naming a location *profile* has not pinned.

        The trip-side twin of ``CommentQuerySet.mentions_all_visible_to``, over
        the same rows - see ``models.comments.location_mention``.

        Args:
            profile: The viewing profile.

        Returns:
            The subset whose every named location the viewer has pinned.
        """
        from urbanlens.dashboard.models.comments.location_mention import CommentLocationMention
        from urbanlens.dashboard.models.pin.model import Pin

        pinned = Pin.objects.filter(profile=profile).exclude(location__isnull=True).values("location__uuid")
        unpinned_mention = CommentLocationMention.objects.filter(trip_comment_id=OuterRef("pk")).exclude(location_uuid__in=pinned)
        return self.filter(~Exists(unpinned_mention))

    def visible_to(self, profile: Profile) -> TripCommentQuerySet:
        """The three gates ``trip_comments.build_comment_tree`` applies, in SQL.

        The queryset counterpart to ``trip_comments.trip_comment_is_visible``,
        and held to it across the product of every visibility setting and every
        relationship by ``test_trip_comment_visibility_is_a_queryset``.

        Answering these in SQL is what lets the panel and the API page a trip
        thread with LIMIT instead of building all of it and slicing the result.
        Identity masking is absent, as it is on the pin/wiki side: it shapes how
        a surviving author is displayed, not whether a row is admitted.

        Args:
            profile: The viewing profile.

        Returns:
            The subset *profile* may see.
        """
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        # permit_null_author: the FK is SET_NULL, and a deleted account has no
        # visibility preference left to enforce - the tree builder's own
        # `c.author is not None and ...` says the same thing.
        author_permits = ProfileModel.visibility_permits_q(profile, author_path="author", visibility_field="comment_visibility", permit_null_author=True)
        unscanned_and_not_mine = Q(pending_scan=True) & ~Q(author_id=profile.pk)
        return self.filter(author_permits).exclude(unscanned_and_not_mine).mentions_all_visible_to(profile)

    def for_member(self, profile: Profile) -> TripCommentQuerySet:
        """Comments on the trips *profile* belongs to.

        The trip ids are resolved to a literal list first rather than left as ``trip__profiles=``
        for the planner to join. Given the join, Postgres is free to drive it from the comment
        table and filter afterwards, which costs the site's comment count rather than the
        viewer's: 30 ms and a sequential scan of 50,000 rows for a viewer belonging to a handful
        of trips, measured on the capacity population (docs/PROBLEMS.md P132).

        Args:
            profile: The viewing profile.

        Returns:
            Comments on trips they are a member of.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        return self.filter(trip__pk__anyof=list(TripMembership.objects.trip_ids_for(profile)))

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
