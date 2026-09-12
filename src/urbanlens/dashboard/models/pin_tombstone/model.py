"""Durable record of pin deletions, for delta-sync clients.
Pins are hard-deleted (recoverable only via the 7-day undo stash, which mints a brand-new uuid on restore - see ``services.undo.handlers.pin``).
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
    #: Unique because pin uuids are never reused (an undo-restore mints a new uuid, so a restored
    #: pin reaches sync clients as a fresh create, not a resurrection of this one).
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
