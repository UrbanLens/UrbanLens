"""Private per-pin notes for the pin owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_NOTE_LENGTH


class PinNote(abstract.DashboardModel):
    """A private, timestamped note that only the pin owner can see.

    Distinct from Pin.description (single editable blob). Notes are append-only
    entries - the owner can delete individual notes but not edit them in place.
    """

    #: max_length adds a MaxLengthValidator without changing the DB column (see
    #: services.core.text_limits) - the note body was previously unbounded on every
    #: write path, which is a storage-abuse vector now that the external API
    #: can create notes too.
    text = TextField(max_length=MAX_PIN_NOTE_LENGTH)

    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="notes",
    )

    if TYPE_CHECKING:
        pin_id: int

    def __str__(self) -> str:
        return f"[{self.pin_id}] {self.text[:60]}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_notes"
        # -pk breaks ties deterministically when two notes land in the same
        # `created` tick (real on fast successive writes - timestamp precision
        # isn't fine enough to guarantee distinct values) - without it, equal
        # timestamps leave "newest first" order up to the database.
        ordering = ["-created", "-pk"]
        indexes = [
            Index(fields=["pin"], name="idxdb_pn_pin"),
        ]
