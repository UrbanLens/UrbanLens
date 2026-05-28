"""Trip models — collaborative trip planning."""

from __future__ import annotations

import logging
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, ForeignKey, ImageField, Index, IntegerField, ManyToManyField, UUIDField
from django.db.models.fields import BooleanField, CharField, DateField, DateTimeField, TextField

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class Trip(abstract.Model):
    """A planned trip shared among one or more users.

    The creator is the user who created the trip. Members includes the creator
    plus any additional users added. Only members can view and edit the trip.
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    start_date = DateField(null=True, blank=True)
    end_date = DateField(null=True, blank=True)

    creator = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="created_trips",
    )
    # All participants including the creator — through TripMembership for RSVP tracking.
    profiles = ManyToManyField(
        "dashboard.Profile",
        blank=True,
        related_name="trips",
        through="TripMembership",
    )

    allow_add_members = BooleanField(default=False, help_text="Non-creator members can add new members.")
    allow_add_activities = BooleanField(default=True, help_text="Non-creator members can add activities.")
    allow_edit_activities = BooleanField(default=False, help_text="Non-creator members can edit or delete activities.")
    allow_comments = BooleanField(default=True, help_text="Comments are enabled for this trip.")

    def __str__(self) -> str:
        return self.name or f"Trip #{self.id}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_trips"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="dashboard_trip_uuid_idx"),
            Index(fields=["start_date"]),
            Index(fields=["end_date"]),
        ]


class TripActivity(abstract.Model):
    """A single planned activity within a trip.

    Each activity is associated with a Location and has an optional scheduled
    date/time and free-form notes.  Activities are ordered by ``order`` within
    a trip so the user can re-sequence them.
    """

    trip = ForeignKey(
        Trip,
        on_delete=CASCADE,
        related_name="activities",
    )
    location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities",
    )
    # Optional link to the adding user's personal Pin (for icon/status context).
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities",
    )
    added_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities_added",
    )
    STATUS_PROPOSED = "proposed"
    STATUS_CONFIRMED = "confirmed"
    STATUS_CHOICES = [
        ("proposed", "Proposed"),
        ("confirmed", "Confirmed"),
    ]

    title = CharField(max_length=255, null=True, blank=True)
    notes = TextField(null=True, blank=True)
    scheduled_at = DateTimeField(null=True, blank=True)
    order = IntegerField(default=0)
    status = CharField(max_length=20, choices=STATUS_CHOICES, default="proposed")

    def __str__(self) -> str:
        loc = self.location.name if self.location else (self.title or "Activity")
        return f"{loc} ({self.trip})"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_trip_activities"
        ordering = ["scheduled_at", "order", "created"]
        indexes = [
            Index(fields=["trip"], name="dashboard_ta_trip_idx"),
            Index(fields=["trip", "scheduled_at"], name="dashboard_ta_trip_dt_idx"),
        ]


class TripMembership(abstract.Model):
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

    trip = ForeignKey(Trip, on_delete=CASCADE, related_name="memberships")
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trip_memberships",
    )
    rsvp = CharField(max_length=20, choices=RSVP_CHOICES, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.profile} in {self.trip} ({self.rsvp or 'no response'})"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_trip_memberships"
        unique_together = [("trip", "profile")]
        indexes = [
            Index(fields=["trip"], name="dashboard_tm_trip_idx"),
        ]
        permissions = [
            ("remove_trip_members", "Can remove members from trips"),
        ]


class TripComment(abstract.Model):
    """A comment left on a trip by one of its members."""

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
        on_delete=CASCADE,
        related_name="replies",
        null=True,
        blank=True,
    )
    text = TextField()
    image = ImageField(upload_to="comment_images/", null=True, blank=True)

    def __str__(self) -> str:
        author = self.author.user.username if self.author and self.author.user else "Unknown"
        return f"[{author}] {self.text[:60]}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_trip_comments"
        ordering = ["created"]
        indexes = [
            Index(fields=["trip"], name="dashboard_tc_trip_idx"),
        ]


class SiteSettings(abstract.Model):
    """Singleton model for site-wide configurable settings.

    Always access via ``SiteSettings.get_current()``; never instantiate directly.
    """

    max_trip_members = IntegerField(
        default=10,
        help_text="Maximum number of members allowed per trip.",
    )

    def __str__(self) -> str:
        return "Site Settings"

    @classmethod
    def get_current(cls) -> SiteSettings:
        """Return (and create if missing) the singleton settings record."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_site_settings"
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"
