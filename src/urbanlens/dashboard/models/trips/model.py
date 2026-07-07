from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import (
    CASCADE,
    SET_NULL,
    FloatField,
    ForeignKey,
    ImageField,
    Index,
    IntegerField,
    JSONField,
    Manager as DjangoManager,
    ManyToManyField,
    UUIDField,
)
from django.db.models.fields import BooleanField, CharField, DateField, DateTimeField, TextField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.trips.queryset import TripManager

if TYPE_CHECKING:
    from datetime import date

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class Trip(abstract.FrontendDashboardModel):
    """A planned trip shared among one or more users.

    The creator is the user who created the trip. Members includes the creator
    plus any additional users added. Only members can view and edit the trip.
    """

    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    start_date = DateField(null=True, blank=True)
    end_date = DateField(null=True, blank=True)

    # TODO: Convert to TextChoices
    PERM_NONE = "none"
    PERM_ORGANIZERS = "organizers"
    PERM_EVERYONE = "everyone"
    PERMISSION_CHOICES = [
        ("none", "No one (creator only)"),
        ("organizers", "Organizers"),
        ("everyone", "Everyone"),
    ]

    allow_add_members = CharField(
        max_length=20,
        choices=PERMISSION_CHOICES,
        default="none",
        help_text="Who can add new members.",
    )
    allow_add_activities = CharField(
        max_length=20,
        choices=PERMISSION_CHOICES,
        default="everyone",
        help_text="Who can add activities.",
    )
    allow_edit_activities = CharField(
        max_length=20,
        choices=PERMISSION_CHOICES,
        default="everyone",
        help_text="Who can edit or delete activities.",
    )
    allow_comments = CharField(
        max_length=20,
        choices=PERMISSION_CHOICES,
        default="everyone",
        help_text="Who can leave comments.",
    )

    creator = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="created_trips",
    )
    # All participants including the creator - through TripMembership for RSVP tracking.
    profiles: ManyToManyField[Profile, Profile] = ManyToManyField(
        "dashboard.Profile",
        blank=True,
        related_name="trips",
        through="TripMembership",
    )

    if TYPE_CHECKING:
        creator_id: int | None
        activities: DjangoManager[TripActivity]

    objects = TripManager()

    def __str__(self) -> str:
        return self.name or f"Trip #{self.id}"

    @property
    def effective_start_date(self) -> date | None:
        """``start_date`` if set, else the earliest scheduled activity's date."""
        if self.start_date:
            return self.start_date
        first = self.activities.filter(scheduled_at__isnull=False).order_by("scheduled_at").first()
        if first is None or first.scheduled_at is None:
            return None
        return first.scheduled_at.date()

    @property
    def effective_end_date(self) -> date | None:
        """``end_date`` if set, else the latest scheduled activity's date."""
        if self.end_date:
            return self.end_date
        last = self.activities.filter(scheduled_at__isnull=False).order_by("-scheduled_at").first()
        if last is None or last.scheduled_at is None:
            return None
        return last.scheduled_at.date()

    @property
    def timeline_status(self) -> str:
        """Coarse timeline label for list cards (`planning`, `upcoming`, `active`, or `past`)."""
        today = timezone.now().date()
        start = self.effective_start_date
        if not start:
            return "planning"
        if start > today:
            return "upcoming"
        end = self.effective_end_date or start
        if end < today:
            return "past"
        return "active"

    @property
    def duration_days(self) -> int | None:
        """Inclusive day count when both start and end dates are set, else ``None``."""
        start, end = self.effective_start_date, self.effective_end_date
        if start and end:
            return (end - start).days + 1
        return None

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trips"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_trip_uuid"),
            Index(fields=["start_date"], name="idxdb_trip_start_date"),
            Index(fields=["end_date"], name="idxdb_trip_end_date"),
        ]


class TripActivity(abstract.DashboardModel):
    """A single planned activity within a trip.

    Each activity is associated with a Location and has an optional scheduled
    date/time and free-form notes.  Activities are ordered by ``order`` within
    a trip so the user can re-sequence them.
    """

    STATUS_PROPOSED = "proposed"
    STATUS_CONFIRMED = "confirmed"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        ("proposed", "Proposed"),
        ("confirmed", "Confirmed"),
        ("completed", "Completed"),
    ]

    title = CharField(max_length=255, null=True, blank=True)
    notes = TextField(null=True, blank=True)
    scheduled_at = DateTimeField(null=True, blank=True)
    scheduled_end = DateTimeField(null=True, blank=True)
    order = IntegerField(default=0)
    status = CharField(max_length=20, choices=STATUS_CHOICES, default="proposed")

    # Map position override - set when user drags the marker; does NOT modify the underlying Pin/Location.
    lat_override = FloatField(null=True, blank=True)
    lng_override = FloatField(null=True, blank=True)

    location_hidden = BooleanField(
        default=False,
        help_text="Hide location from the map. The activity still appears in the list as 'Secret Location'.",
    )

    trip = ForeignKey(
        Trip,
        on_delete=CASCADE,
        related_name="activities",
    )
    # TODO: Reassess linking both location and pin.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities",
    )
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities",
    )
    # Optional link to a child trip (its activities appear on the parent map).
    child_trip = ForeignKey(
        Trip,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="parent_activities",
    )
    added_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities_added",
    )

    if TYPE_CHECKING:
        location_id: int | None
        child_trip_id: int | None
        added_by_id: int | None
        trip_id: int | None
        pin_id: int | None

    @property
    def effective_title(self) -> str:
        """Display label: custom title, linked pin name/address, location name/address, or fallback."""
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        if self.title:
            return self.title
        if self.pin:
            pin_label = self.pin.display_label
            if pin_label:
                return pin_label
        if self.location:
            name = self.location.display_name
            if is_meaningful_name(name):
                return name
            if self.location.address:
                return self.location.address
        return "Unnamed activity"

    def __str__(self) -> str:
        return f"{self.effective_title} ({self.trip})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trip_activities"
        ordering = ["scheduled_at", "order", "created"]
        indexes = [
            Index(fields=["trip"], name="idxdb_ta_trip"),
            Index(fields=["trip", "scheduled_at"], name="idxdb_ta_trip_dt"),
        ]


class TripMembership(abstract.DashboardModel):
    """RSVP through-model linking a Profile to a Trip.

    Replaces the implicit M2M join table so each membership can carry an RSVP
    status independently of whether the person is in or out of the trip.
    """

    RSVP_YES = "yes"
    RSVP_NO = "no"
    RSVP_MAYBE = "maybe"
    RSVP_CHOICES = [
        ("yes", "Yes"),
        ("no", "No"),
        ("maybe", "Maybe"),
    ]

    rsvp = CharField(max_length=20, choices=RSVP_CHOICES, null=True, blank=True)
    is_organizer = BooleanField(
        default=False,
        help_text="Organizers have the same trip-management rights as the creator.",
    )

    trip = ForeignKey(Trip, on_delete=CASCADE, related_name="memberships")
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trip_memberships",
    )

    def __str__(self) -> str:
        return f"{self.profile} in {self.trip} ({self.rsvp or 'no response'})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trip_memberships"
        unique_together = [("trip", "profile")]
        indexes = [
            Index(fields=["trip"], name="idxdb_tm_trip"),
        ]
        permissions = [
            ("remove_trip_members", "Can remove members from trips"),
        ]


class TripComment(abstract.DashboardModel):
    """A comment left on a trip by one of its members."""

    text = TextField()
    image = ImageField(upload_to="comment_images/", null=True, blank=True)
    map_data = JSONField(null=True, blank=True)
    
    trip = ForeignKey(
        Trip,
        on_delete=CASCADE,
        related_name="comments",
    )
    author = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_comments",
    )
    parent = ForeignKey(
        "self",
        on_delete=SET_NULL,
        related_name="replies",
        null=True,
        blank=True,
    )

    def __str__(self) -> str:
        author = self.author.user.username if self.author and self.author.user else "Unknown"
        return f"[{author}] {self.text[:60]}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trip_comments"
        ordering = ["created"]
        indexes = [
            Index(fields=["trip"], name="idxdb_tc_trip"),
        ]


class TripActivityVote(abstract.DashboardModel):
    """A member's thumbs-up or thumbs-down vote on a proposed activity.

    Only one vote per (activity, profile) pair is allowed. Votes are only
    meaningful while the activity is in the 'proposed' status.
    """

    # TODO: Convert to TextChoices
    VOTE_UP = "up"
    VOTE_DOWN = "down"
    VOTE_CHOICES = [
        ("up", "Up"),
        ("down", "Down"),
    ]
    vote = CharField(max_length=4, choices=VOTE_CHOICES)

    activity = ForeignKey(
        TripActivity,
        on_delete=CASCADE,
        related_name="votes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="activity_votes",
    )

    def __str__(self) -> str:
        return f"{self.profile} {self.vote} on {self.activity}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trip_activity_votes"
        unique_together = [("activity", "profile")]
        indexes = [
            Index(fields=["activity"], name="idxdb_tav_activity"),
        ]
