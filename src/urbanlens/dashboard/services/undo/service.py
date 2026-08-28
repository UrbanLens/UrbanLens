"""Stash/restore for the generic undo framework.

Deleting a model instance cascades to its DB-level children before any of
this gets a chance to run - see the per-model docstrings under
``services.undo.handlers`` for exactly what is and isn't restorable for each
model. ``dashboard.models.undo.UndoAction`` holds the serialized payload
directly (see that model's docstring for why this isn't cache-backed).

Mutations (a pin move, a label add, an album membership change) stash a
before/after payload instead of a deleted-row snapshot. Undoing stamps
``undone_at`` rather than deleting the row, so the same payload can be
applied forward again (redo). A new stash discards the redo stack: the
history has forked.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.undo import UNDO_RETENTION, UndoAction, UndoKind
from urbanlens.dashboard.services.undo import handlers as _handlers
from urbanlens.dashboard.services.undo.base import get_handler

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from django.db.models import Model, QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Set while an undo/redo is applying, so the write path being inverted does
#: not stash a nested undo of the undo.
_APPLYING: contextvars.ContextVar[bool] = contextvars.ContextVar("undo_applying", default=False)


class UndoExpiredError(Exception):
    """Raised when an UndoAction is past its retention window."""


class UndoAlreadyRestoredError(UndoExpiredError):
    """Raised when an UndoAction was already restored by another request.

    Subclasses :class:`UndoExpiredError` so the existing callers - which all
    answer "this undo is no longer available" - keep working unchanged, while
    a caller that wants to tell the two apart still can.
    """


class NothingToUndoError(Exception):
    """Raised when the undo (or redo) stack is empty."""


@contextlib.contextmanager
def applying_undo() -> Iterator[None]:
    """Suppress nested stashes while an undo or redo is being applied."""
    token = _APPLYING.set(True)
    try:
        yield
    finally:
        _APPLYING.reset(token)


def _repr_limit() -> int:
    """Width of ``UndoAction.object_repr``, so describe() cannot overflow it."""
    return UndoAction._meta.get_field("object_repr").max_length  # noqa: SLF001 - _meta is public API


def _truncate_repr(text: str) -> str:
    return text[: _repr_limit()]


def discard_redo_stack(profile: Profile) -> int:
    """Drop every undone entry for ``profile`` (the history has forked).

    Args:
        profile: The profile whose redo stack should be cleared.

    Returns:
        How many entries were discarded.
    """
    pending = UndoAction.objects.for_profile(profile).redoable()
    count = pending.count()
    pending.delete()
    return count


def stash_for_undo(model_label: str, instances: Sequence[Model], profile: Profile) -> UndoAction | None:
    """Serialize ``instances`` and index them for a profile's undo history.

    Must be called before the instances are deleted.

    Args:
        model_label: Registry key of the handler to use (e.g. ``"pin"``).
        instances: The instances about to be deleted.
        profile: The profile performing (and who may later undo) the delete.

    Returns:
        The created UndoAction row, or None when called from inside an
        undo/redo apply (the inverted write must not stash itself).
    """
    if _APPLYING.get():
        return None
    handler = get_handler(model_label)
    discard_redo_stack(profile)
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
    return UndoAction.objects.create(
        profile=profile,
        model_label=model_label,
        kind=UndoKind.DELETE,
        object_repr=_truncate_repr(handler.describe(instances)),
        payload=handler.serialize(instances),
    )


def stash_mutation(model_label: str, profile: Profile, *, payload: dict[str, Any], description: str) -> UndoAction | None:
    """Record a reversible change for a profile's undo history.

    Args:
        model_label: Registry key of the mutation handler to use.
        profile: The profile performing (and who may later undo) the change.
        payload: JSON-safe dict the handler's ``undo_mutation``/``redo_mutation``
            will apply.
        description: Short label shown on the undo button and history list.

    Returns:
        The created UndoAction row, or None when called from inside an
        undo/redo apply.
    """
    if _APPLYING.get():
        return None
    get_handler(model_label)  # fail closed if the label is unregistered
    discard_redo_stack(profile)
    return UndoAction.objects.create(
        profile=profile,
        model_label=model_label,
        kind=UndoKind.MUTATE,
        object_repr=_truncate_repr(description),
        payload=payload,
    )


def _lock(undo_action: UndoAction) -> UndoAction:
    """Lock ``undo_action``'s row for the duration of the current transaction.

    Returns:
        The locked row.

    Raises:
        UndoAlreadyRestoredError: The row was deleted between lookup and lock.
    """
    claimed = UndoAction.objects.select_for_update().filter(pk=undo_action.pk).first()
    if claimed is None:
        raise UndoAlreadyRestoredError(f"UndoAction {undo_action.pk} was already restored.")
    return claimed


def restore_undo_action(undo_action: UndoAction) -> list[Any]:
    """Undo ``undo_action``: recreate a delete, or invert a mutation.

    The entry is stamped with ``undone_at`` rather than deleted, so it can be
    redone. Callers are responsible for checking it belongs to the requesting
    profile before calling this.

    Args:
        undo_action: The entry to undo.

    Returns:
        Recreated instances for a delete; an empty list for a mutation.

    Raises:
        UndoExpiredError: Past the retention window, or already undone.
    """
    handler = get_handler(undo_action.model_label)
    expired_pk: int | None = None
    with transaction.atomic(), applying_undo():
        claimed = _lock(undo_action)
        if claimed.is_expired:
            expired_pk = claimed.pk
            claimed.delete()
        elif claimed.undone_at is not None:
            raise UndoAlreadyRestoredError(f"UndoAction {claimed.pk} was already restored.")
        else:
            restored: list[Any] = []
            if claimed.kind == UndoKind.MUTATE:
                payload = claimed.payload if isinstance(claimed.payload, dict) else {}
                handler.undo_mutation(payload)
                claimed.payload = payload
            else:
                raw = claimed.payload
                entries = raw["entries"] if isinstance(raw, dict) and "entries" in raw else raw
                restored = handler.restore(entries)
                claimed.payload = {"entries": entries, "restored_pks": [obj.pk for obj in restored]}
            claimed.undone_at = timezone.now()
            claimed.save(update_fields=["payload", "undone_at"])
            return restored
    raise UndoExpiredError(f"UndoAction {expired_pk} is past its {UNDO_RETENTION.days}-day retention window.")


def redo_undo_action(undo_action: UndoAction) -> None:
    """Re-apply an entry that was previously undone.

    Args:
        undo_action: The entry to redo. Must have ``undone_at`` set.

    Raises:
        UndoExpiredError: Past the retention window, or not currently undone.
    """
    handler = get_handler(undo_action.model_label)
    expired_pk: int | None = None
    with transaction.atomic(), applying_undo():
        claimed = _lock(undo_action)
        if claimed.is_expired:
            expired_pk = claimed.pk
            claimed.delete()
        elif claimed.undone_at is None:
            raise UndoAlreadyRestoredError(f"UndoAction {claimed.pk} is not waiting to be redone.")
        else:
            if claimed.kind == UndoKind.MUTATE:
                payload = claimed.payload if isinstance(claimed.payload, dict) else {}
                handler.redo_mutation(payload)
                claimed.payload = payload
            else:
                payload = claimed.payload if isinstance(claimed.payload, dict) else {}
                handler.redo_delete(payload)
            claimed.undone_at = None
            claimed.save(update_fields=["payload", "undone_at"])
            return
    raise UndoExpiredError(f"UndoAction {expired_pk} is past its {UNDO_RETENTION.days}-day retention window.")


def peek_undo(profile: Profile) -> UndoAction | None:
    """The newest still-undoable entry for ``profile``, or None."""
    return UndoAction.objects.for_profile(profile).active().undoable().first()


def peek_redo(profile: Profile) -> UndoAction | None:
    """The most recently undone entry for ``profile``, or None."""
    return UndoAction.objects.for_profile(profile).active().redoable().order_by("-undone_at").first()


def stack_state(profile: Profile) -> dict[str, Any]:
    """JSON-safe snapshot of whether undo/redo are currently possible.

    Args:
        profile: The profile whose stacks to inspect.

    Returns:
        ``can_undo``/``can_redo`` plus labels for the buttons.
    """
    undo_entry = peek_undo(profile)
    redo_entry = peek_redo(profile)
    return {
        "can_undo": undo_entry is not None,
        "can_redo": redo_entry is not None,
        "undo_label": undo_entry.object_repr if undo_entry else None,
        "redo_label": redo_entry.object_repr if redo_entry else None,
        "undo_uuid": str(undo_entry.uuid) if undo_entry else None,
        "redo_uuid": str(redo_entry.uuid) if redo_entry else None,
    }


def undo_latest(profile: Profile) -> list[Any]:
    """Undo the newest entry on ``profile``'s stack.

    Raises:
        NothingToUndoError: The undo stack is empty.
        UndoExpiredError: The top entry expired between peek and claim.
    """
    action = peek_undo(profile)
    if action is None:
        raise NothingToUndoError("Nothing to undo.")
    return restore_undo_action(action)


def redo_latest(profile: Profile) -> None:
    """Redo the most recently undone entry on ``profile``'s stack.

    Raises:
        NothingToUndoError: The redo stack is empty.
        UndoExpiredError: The top entry expired between peek and claim.
    """
    action = peek_redo(profile)
    if action is None:
        raise NothingToUndoError("Nothing to redo.")
    redo_undo_action(action)


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
    """Return this profile's active, not-yet-undone entries, newest first."""
    return UndoAction.objects.for_profile(profile).active().undoable()
