"""Stash/restore for the generic undo-delete framework.

Deleting a model instance cascades to its DB-level children before any of
this gets a chance to run - see the per-model docstrings under
``services.undo.handlers`` for exactly what is and isn't restorable for each
model. ``dashboard.models.undo.UndoAction`` holds the serialized payload
directly (see that model's docstring for why this isn't cache-backed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

from urbanlens.dashboard.models.undo import UNDO_RETENTION, UndoAction
from urbanlens.dashboard.services.undo import handlers as _handlers
from urbanlens.dashboard.services.undo.base import get_handler

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import Model, QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class UndoExpiredError(Exception):
    """Raised when an UndoAction is past its retention window."""


class UndoAlreadyRestoredError(UndoExpiredError):
    """Raised when an UndoAction was already restored by another request.

    Subclasses :class:`UndoExpiredError` so the existing callers - which all
    answer "this undo is no longer available" - keep working unchanged, while
    a caller that wants to tell the two apart still can.
    """


def stash_for_undo(model_label: str, instances: Sequence[Model], profile: Profile) -> UndoAction:
    """Serialize ``instances`` and index them for a profile's undo history.

    Must be called before the instances are deleted.

    Args:
        model_label: Registry key of the handler to use (e.g. ``"pin"``).
        instances: The instances about to be deleted.
        profile: The profile performing (and who may later undo) the delete.

    Returns:
        The created UndoAction row.
    """
    handler = get_handler(model_label)
    # Truncated to the column's own width rather than a literal, so the two
    # cannot drift. `describe()` wraps a user-supplied name in fixed text, and
    # several of those names are themselves 255 characters (Label.name,
    # Pin.name) - the same width as this column. So a perfectly legal name
    # overflowed `object_repr` and the DataError surfaced as a 500 on *delete*:
    # the user could create the object but never remove it. Found by the
    # write-route smoke sweep on `label.delete` (PROBLEMS.md, 2026-08-16).
    #
    # Fixed here rather than in each handler because every model's delete path
    # funnels through this one call, so one truncation covers all of them - and
    # a handler added later inherits it.
    repr_limit = UndoAction._meta.get_field("object_repr").max_length  # noqa: SLF001 - _meta is public API
    return UndoAction.objects.create(
        profile=profile,
        model_label=model_label,
        object_repr=handler.describe(instances)[:repr_limit],
        payload=handler.serialize(instances),
    )


def restore_undo_action(undo_action: UndoAction) -> list[Any]:
    """Recreate the instance(s) stashed by ``undo_action`` and remove the entry.

    Args:
        undo_action: The entry to restore. Callers are responsible for
            checking it belongs to the requesting profile before calling this.

    Returns:
        The recreated instances.

    Raises:
        UndoExpiredError: If the entry is past its ``UNDO_RETENTION`` window -
            the stale row is deleted before this is raised. Also raised by a
            handler's own ``restore()`` when a foreign key it needs to recreate
            a row (e.g. a profile, label, or wiki creator) was independently
            deleted during the retention window.
    """
    if undo_action.is_expired:
        undo_action.delete()
        raise UndoExpiredError(f"UndoAction {undo_action.pk} is past its {UNDO_RETENTION.days}-day retention window.")

    handler = get_handler(undo_action.model_label)
    with transaction.atomic():
        # Claim the row before restoring. Expiry was checked against an instance
        # the caller fetched earlier, and a double-submitted Undo gives two
        # requests a valid instance each: without the lock both pass that check
        # and both restore, so one click brings everything back twice. Locking
        # here makes the second request wait and then find the row gone.
        claimed = UndoAction.objects.select_for_update().filter(pk=undo_action.pk).first()
        if claimed is None:
            raise UndoAlreadyRestoredError(f"UndoAction {undo_action.pk} was already restored.")
        # A handler's restore() may raise UndoExpiredError partway through a
        # multi-instance batch (e.g. the second of three stashed pins
        # references a label that's since been deleted) - wrapping in
        # transaction.atomic() ensures that failure can't leave the first
        # instance restored while the UndoAction itself still gets deleted
        # below, which would otherwise silently orphan a partially-restored
        # batch with no surviving undo entry to retry from.
        restored = handler.restore(claimed.payload)
        claimed.delete()
    return restored


def clear_undo_history(profile: Profile) -> int:
    """Delete every undo entry for ``profile``.

    Args:
        profile: The profile whose undo history should be cleared.

    Returns:
        Number of entries cleared.
    """
    count = UndoAction.objects.for_profile(profile).count()
    UndoAction.objects.for_profile(profile).delete()
    return count


def get_undo_history(profile: Profile) -> QuerySet[UndoAction]:
    """Return this profile's active (non-expired) undo entries, newest first."""
    return UndoAction.objects.for_profile(profile).active()
