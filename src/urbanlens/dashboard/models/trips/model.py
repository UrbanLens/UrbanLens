"""Trip models — collaborative trip planning."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, ForeignKey, Index, IntegerField, ManyToManyField, UUIDField
from django.db.models.fields import CharField, DateField, DateTimeField, TextField

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

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

    creator: Profile | None = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="created_trips",
    )
    # All participants including the creator.
    profiles = ManyToManyField(
        "dashboard.Profile",
        blank=True,
        related_name="trips",
    )

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

    trip: Trip = ForeignKey(
        Trip,
        on_delete=CASCADE,
        related_name="activities",
    )
    location: Location | None = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities",
    )
    # Optional link to the adding user's personal Pin (for icon/status context).
    pin: Pin | None = ForeignKey(
        "dashboard.Pin",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities",
    )
    added_by: Profile | None = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_activities_added",
    )
    title = CharField(max_length=255, null=True, blank=True)
    notes = TextField(null=True, blank=True)
    scheduled_at = DateTimeField(null=True, blank=True)
    order = IntegerField(default=0)

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


class TripComment(abstract.Model):
    """A comment left on a trip by one of its members."""

    trip: Trip = ForeignKey(
        Trip,
        on_delete=CASCADE,
        related_name="comments",
    )
    author: Profile | None = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trip_comments",
    )
    text = TextField()

    def __str__(self) -> str:
        author = self.author.user.username if self.author and self.author.user else "Unknown"
        return f"[{author}] {self.text[:60]}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_trip_comments"
        ordering = ["created"]
        indexes = [
            Index(fields=["trip"], name="dashboard_tc_trip_idx"),
        ]
