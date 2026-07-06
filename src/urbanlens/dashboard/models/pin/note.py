"""Private per-pin notes for the pin owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index, TextField

from urbanlens.dashboard.models import abstract


class PinNote(abstract.Model):
    """A private, timestamped note that only the pin owner can see.

    Distinct from Pin.description (single editable blob). Notes are append-only
    entries - the owner can delete individual notes but not edit them in place.
    """

    text = TextField()

    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="notes",
    )

    if TYPE_CHECKING:
        pin_id: int

    def __str__(self) -> str:
        return f"[{self.pin_id}] {self.text[:60]}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_pin_notes"
        ordering = ["-created"]
        indexes = [
            Index(fields=["pin"], name="idxdb_pn_pin"),
        ]
