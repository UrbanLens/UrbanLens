"""UndoAction - a durable, restorable record of a deleted object (or batch).

The serialized payload needed to restore the delete lives directly on this
row (``payload``), not in a cache: a cache entry can vanish well before its
nominal TTL for reasons that have nothing to do with elapsed time (no shared
Redis/Valkey configured, so Django falls back to a per-process locmem cache
that a different worker/process can't see; or the entry gets evicted early
under memory pressure on a shared cache instance) - which previously showed
up as an undo entry that still listed as recent and un-expired, but silently
failed with "no longer available" the moment it was actually restored.
Storing the payload in the same durable row as the rest of the undo index
removes that whole failure mode: this row's own ``created`` timestamp is the
single source of truth for whether it's still restorable.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import CASCADE, ForeignKey, Index, JSONField
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
        payload: The JSON-safe snapshot produced by the handler's
            ``serialize()``, needed to recreate the deleted instance(s).
        profile: The profile who performed the delete and may restore it.
    """

    model_label = CharField(max_length=50)
    object_repr = CharField(max_length=255)
    # DjangoJSONEncoder because handlers snapshot model fields as-is, and some
    # (SafetyCheckin's checkin_by/escalated_at/... datetimes and grace_period
    # duration) aren't plain-JSON types. The cache this payload used to live
    # in pickled values, so raw datetimes round-tripped silently; a bare
    # JSONField made every such delete crash at stash time instead. Restore
    # feeds the ISO strings back through normal model-field coercion
    # (DateTimeField/DurationField.to_python), so no decoder is needed.
    payload = JSONField(encoder=DjangoJSONEncoder)

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
