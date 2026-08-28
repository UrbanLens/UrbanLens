"""UndoAction - a durable, restorable record of a destructive action.

The serialized payload needed to undo the action lives directly on this
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

``undone_at`` is the redo stack: undoing an entry stamps it rather than
deleting it, so the same payload can be applied forward again. A new action
discards every stamped entry (the history has forked).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import CASCADE, ForeignKey, Index, JSONField
from django.db.models.fields import CharField, DateTimeField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.undo.queryset import UndoActionManager

UNDO_RETENTION = timedelta(days=7)


class UndoKind(TextChoices):
    """Whether this entry restores a deletion or reverts a mutation."""

    DELETE = "delete", "Delete"
    MUTATE = "mutate", "Mutate"


class UndoAction(abstract.FrontendDashboardModel):
    """One restorable action a profile can undo (and then redo).

    Attributes:
        model_label: Registry key of the ``UndoHandler`` (see
            ``services.undo.handlers``) that knows how to restore this entry.
        kind: ``delete`` recreates stashed rows; ``mutate`` applies the inverse
            of a field/membership change.
        object_repr: Human-readable label shown in the undo history list.
        payload: The JSON-safe snapshot produced by the handler, needed to
            undo (and later redo) the action.
        undone_at: Set when this entry has been undone and is waiting on the
            redo stack. Null means it is still undoable.
        profile: The profile who performed the action and may undo it.
    """

    model_label = CharField(max_length=50)
    kind = CharField(max_length=12, choices=UndoKind.choices, default=UndoKind.DELETE)
    object_repr = CharField(max_length=255)
    # DjangoJSONEncoder because handlers snapshot model fields as-is, and some
    # (SafetyCheckin's checkin_by/escalated_at/... datetimes and grace_period
    # duration) aren't plain-JSON types. The cache this payload used to live
    # in pickled values, so raw datetimes round-tripped silently; a bare
    # JSONField made every such delete crash at stash time instead. Restore
    # feeds the ISO strings back through normal model-field coercion
    # (DateTimeField/DurationField.to_python), so no decoder is needed.
    payload = JSONField(encoder=DjangoJSONEncoder)
    undone_at = DateTimeField(null=True, blank=True)

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

    @property
    def is_undone(self) -> bool:
        """Whether this entry has been undone and is waiting on the redo stack."""
        return self.undone_at is not None

    def __str__(self) -> str:
        return f"{self.object_repr} (undo for profile {self.profile_id})"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_undo_actions"
        ordering = ["-created"]
        indexes = [
            Index(fields=["profile", "created"], name="idxdb_undo_profile_created"),
            Index(fields=["profile", "undone_at", "created"], name="idxdb_undo_profile_undone"),
        ]
