"""PinVisit model - records each visit a user made to a pinned location."""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, CharField, DateTimeField, ForeignKey, Index, TextChoices, TextField, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.visits.queryset import VisitManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class VisitSource(TextChoices):
    """Origin of a PinVisit record."""

    MANUAL = "manual", "Manual"
    GOOGLE_TAKEOUT = "google_takeout", "Google Takeout"


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
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="visit_history",
    )
    visited_at = DateTimeField()
    notes = TextField(null=True, blank=True)
    source = CharField(max_length=20, choices=VisitSource.choices, default=VisitSource.MANUAL)

    objects = VisitManager()

    if TYPE_CHECKING:
        pin_id: int

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
            Index(fields=["uuid"], name="dashboard_pv_uuid_idx"),
            Index(fields=["pin"], name="dashboard_pv_pin_idx"),
            Index(fields=["pin", "visited_at"], name="dashboard_pv_pin_visited_idx"),
        ]
