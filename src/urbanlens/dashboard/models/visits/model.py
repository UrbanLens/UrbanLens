"""PinVisit model - records each visit a user made to a pinned location."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, ForeignKey, Index, JSONField, ManyToManyField, TextChoices, TextField, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.visits.queryset import VisitManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class VisitSource(TextChoices):
    """Origin of a PinVisit record.

    - MANUAL: User manually added the visit.
    - HISTORY: Imported from the user's location history.
    - TRIP: Added from a trip the user attended.
    - USER: Added by another user.
    - PHOTO: Added when the user uploaded a photo with location metadata.
    - GEOLOCATION: Added when the user's device provided a geolocation.
    - SAFETY_CHECKIN: Added when a safety check-in concluded at this place.
    """

    MANUAL = "manual", "Manual"
    HISTORY = "history", "History"
    TRIP = "trip", "Trip"
    USER = "user", "User"
    PHOTO = "photo", "Photo"
    GEOLOCATION = "geolocation", "Geolocation"
    SAFETY_CHECKIN = "safety_checkin", "Safety Check-in"


class PinVisit(abstract.Model):
    """A single recorded visit by a user to one of their pinned locations.

    Multiple PinVisit rows can exist per pin. When a visit is created or deleted
    through the controller, pin.last_visited is kept in sync with the most
    recent visited_at across all visit records for that pin.

    Attributes:
        pin: The pin this visit belongs to.
        visited_at: When the visit occurred.
        notes: Optional free-text note about the visit.
        source: Whether this was entered manually or imported from Google Takeout.
        participants: Other profiles the pin owner says were present for this visit.
        map_data: Optional Leaflet map snapshot (centre, zoom, freehand markup)
            the user drew to document the visit. Same schema as
            ``Comment.map_data``; sanitized via ``services.map_snapshot``.
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    visited_at = DateTimeField()
    notes = TextField(null=True, blank=True)
    source = CharField(max_length=20, choices=VisitSource.choices, default=VisitSource.MANUAL)
    map_data = JSONField(null=True, blank=True)

    participants = ManyToManyField(
        "dashboard.Profile",
        blank=True,
        related_name="visit_participations",
    )
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="visit_history",
    )
    route = ForeignKey(
        "dashboard.Route",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="visits",
    )

    objects = VisitManager()

    if TYPE_CHECKING:
        pin_id: int
        route_id: int | None

    def __str__(self) -> str:
        """Return a human-readable description of this visit.

        Returns:
            String like "Visit to <pin_id> on YYYY-MM-DD".
        """
        return f"Visit to {self.pin_id} on {self.visited_at:%Y-%m-%d}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_pin_visits"
        ordering = ["-visited_at"]
        get_latest_by = "visited_at"
        indexes = [
            Index(fields=["uuid"], name="idxdb_pv_uuid"),
            Index(fields=["pin"], name="idxdb_pv_pin"),
            Index(fields=["pin", "visited_at"], name="idxdb_pv_pin_visited"),
        ]
