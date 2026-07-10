"""UndoAction - a lightweight DB index over cache-backed delete-undo entries.

The row itself never holds the serialized payload - that lives in the cache
(see ``services.undo``), keyed by ``cache_key``, with a TTL matching
``UNDO_RETENTION``. This table exists purely so a profile's undo history can
be listed, paginated, and cleared reliably: Redis/Valkey offers no cheap way
to enumerate keys by owner at scale, and the in-process locmem cache fallback
can't be enumerated at all.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index
from django.db.models.fields import CharField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.undo.queryset import UndoActionManager

UNDO_RETENTION = timedelta(days=7)


class UndoAction(abstract.FrontendDashboardModel):
    """One deleted object (or batch) a profile can still restore.

    Attributes:
        model_label: Registry key of the ``UndoHandler`` (see
            ``services.undo.handlers``) that knows how to restore this entry.
        object_repr: Human-readable label shown in the undo history list.
        cache_key: Token identifying the serialized payload in the cache.
        profile: The profile who performed the delete and may restore it.
    """

    model_label = CharField(max_length=50)
    object_repr = CharField(max_length=255)
    cache_key = CharField(max_length=64, unique=True)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="undo_actions")

    if TYPE_CHECKING:
        profile_id: int

    objects = UndoActionManager()

    @property
    def expires_at(self):
        """When this undo entry stops being restorable."""
        return self.created + UNDO_RETENTION

    @property
    def is_expired(self) -> bool:
        """Whether this entry is past its retention window."""
        return timezone.now() >= self.expires_at

    def __str__(self) -> str:
        return f"{self.object_repr} (undo for profile {self.profile_id})"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_undo_actions"
        ordering = ["-created"]
        indexes = [
            Index(fields=["profile", "created"], name="idxdb_undo_profile_created"),
        ]
