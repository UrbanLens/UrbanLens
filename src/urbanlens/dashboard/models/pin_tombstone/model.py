"""Durable record of pin deletions, for delta-sync clients.

Pins are hard-deleted (recoverable only via the 7-day undo stash, which mints
a brand-new uuid on restore - see ``services.undo.handlers.pin``). Nothing
else in the system durably records that a deletion happened: the map's
``last_updated`` change signal is ``Max(updated)`` over the *remaining* pins,
which a deletion doesn't move. A client syncing incrementally over the
external API would therefore never learn that a pin it holds locally is gone.

A ``PinTombstone`` row closes that gap: written in the same transaction as
the pin's delete (see ``models.pin.signals.record_pin_tombstone``), it lets
the external API's ``pins/deleted/`` endpoint answer "which of my pins were
deleted since <timestamp>". Rows contain nothing but the pin's public uuid -
no name, notes, or coordinates survive the deletion.

Tombstones are kept indefinitely for now; ``PinTombstoneManager.prune_older_than``
exists for a future scheduled pruning task, whose retention must stay longer
than the longest supported client offline window (a client older than the
oldest tombstone must full-resync).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_tombstone.queryset import PinTombstoneManager


class PinTombstone(abstract.DashboardModel):
    """One deleted pin: its public uuid and (via ``created``) when it was deleted."""

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="pin_tombstones")
    #: The deleted pin's public uuid - the identifier sync clients key on.
    #: Unique because pin uuids are never reused (an undo-restore mints a new
    #: uuid, so a restored pin reaches sync clients as a fresh create, not a
    #: resurrection of this one).
    pin_uuid = UUIDField(unique=True, editable=False)

    if TYPE_CHECKING:
        id: int
        profile_id: int

    objects = PinTombstoneManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_tombstones"
        ordering = ["created"]
        indexes = [
            Index(fields=["profile", "created"], name="idxdb_pintomb_prof_created"),
        ]

    def __str__(self) -> str:
        return f"PinTombstone(profile={self.profile_id}, pin_uuid={self.pin_uuid})"
