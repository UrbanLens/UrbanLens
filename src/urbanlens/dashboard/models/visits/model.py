"""PinVisit model - records each visit a user made to a pinned location."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, DateTimeField, ForeignKey, Index, ManyToManyField, TextChoices, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.visits.queryset import VisitManager


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

    MANUAL = "manual", "Journal"
    HISTORY = "history", "Imported"
    TRIP = "trip", "Trip"
    USER = "user", "A Friend's Journal"
    PHOTO = "photo", "Photo"
    GEOLOCATION = "geolocation", "Geolocation"
    SAFETY_CHECKIN = "safety_checkin", "Safety Check-in"


class PinVisit(abstract.FrontendDashboardModel):
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
        markup_map: Optional standalone MarkupMap (viewport + markup items)
            the user drew to document the visit.
    """

    visited_at = DateTimeField()
    notes = TextField(null=True, blank=True)
    source = CharField(max_length=20, choices=VisitSource.choices, default=VisitSource.MANUAL)
    markup_map = ForeignKey(
        "dashboard.MarkupMap",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="visits",
    )
    tentative = BooleanField(default=False)

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
        markup_map_id: int | None

    @property
    def map_data(self) -> dict | None:
        """Client snapshot of the attached markup map, if any.

        Kept as a property so templates and viewer JS that consumed the old
        ``map_data`` JSON column keep working against the MarkupMap relation.

        Returns:
            Snapshot dict or None when no map is attached.
        """
        return self.markup_map.to_snapshot() if self.markup_map else None

    def __str__(self) -> str:
        """Return a human-readable description of this visit.

        Returns:
            String like "Visit to <pin_id> on YYYY-MM-DD".
        """
        return f"Visit to {self.pin_id} on {self.visited_at:%Y-%m-%d}"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_pin_visits"
        ordering = ["-visited_at"]
        get_latest_by = "visited_at"
        indexes = [
            Index(fields=["uuid"], name="idxdb_pv_uuid"),
            Index(fields=["pin"], name="idxdb_pv_pin"),
            Index(fields=["pin", "tentative"], name="idxdb_pv_pin_tent"),
            Index(fields=["pin", "visited_at"], name="idxdb_pv_pin_vat"),
            Index(fields=["pin", "visited_at", "tentative"], name="idxdb_pv_pin_vat_tent"),
        ]
