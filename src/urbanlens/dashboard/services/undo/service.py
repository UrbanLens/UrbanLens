"""Cache-backed stash/restore for the generic undo-delete framework.

Deleting a model instance cascades to its DB-level children before any of
this gets a chance to run - see the per-model docstrings under
``services.undo.handlers`` for exactly what is and isn't restorable for each
model. ``dashboard.models.undo.UndoAction`` is the DB-side index that lets a
profile's undo history be listed and cleared reliably regardless of cache
backend (Redis/Valkey offers no cheap way to enumerate keys by owner at
scale, and the locmem fallback can't be enumerated at all).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
import uuid

from django.core.cache import cache

from urbanlens.dashboard.models.undo import UNDO_RETENTION, UndoAction
from urbanlens.dashboard.services.undo import handlers as _handlers
from urbanlens.dashboard.services.undo.base import get_handler

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

UNDO_TTL_SECONDS = int(UNDO_RETENTION.total_seconds())


class UndoExpiredError(Exception):
    """Raised when the cached payload for an UndoAction is gone (evicted, or past its TTL)."""


def _cache_key(token: str) -> str:
    return f"dashboard:undo:{token}"


def stash_for_undo(model_label: str, instances: list[Model], profile: Profile) -> UndoAction:
    """Serialize ``instances`` into the cache and index them for a profile's undo history.

    Must be called before the instances are deleted.

    Args:
        model_label: Registry key of the handler to use (e.g. ``"pin"``).
        instances: The instances about to be deleted.
        profile: The profile performing (and who may later undo) the delete.

    Returns:
        The created UndoAction row.
    """
    handler = get_handler(model_label)
    payload = handler.serialize(instances)
    token = uuid.uuid4().hex
    cache.set(_cache_key(token), payload, timeout=UNDO_TTL_SECONDS)
    return UndoAction.objects.create(
        profile=profile,
        model_label=model_label,
        object_repr=handler.describe(instances),
        cache_key=token,
    )


def restore_undo_action(undo_action: UndoAction) -> list[Any]:
    """Recreate the instance(s) stashed by ``undo_action`` and remove the entry.

    Args:
        undo_action: The entry to restore. Callers are responsible for
            checking it belongs to the requesting profile before calling this.

    Returns:
        The recreated instances.

    Raises:
        UndoExpiredError: If the cached payload is missing (evicted, or the
            entry is past its retention window) - the stale row is deleted
            before this is raised.
    """
    key = _cache_key(undo_action.cache_key)
    payload = cache.get(key)
    if payload is None:
        undo_action.delete()
        raise UndoExpiredError(f"Undo payload for UndoAction {undo_action.pk} is no longer available.")

    handler = get_handler(undo_action.model_label)
    restored = handler.restore(payload)
    cache.delete(key)
    undo_action.delete()
    return restored


def clear_undo_history(profile: Profile) -> int:
    """Delete every undo entry for ``profile``, including its cached payloads.

    Args:
        profile: The profile whose undo history should be cleared.

    Returns:
        Number of entries cleared.
    """
    actions = list(UndoAction.objects.for_profile(profile))
    for action in actions:
        cache.delete(_cache_key(action.cache_key))
    UndoAction.objects.filter(pk__in=[a.pk for a in actions]).delete()
    return len(actions)


def get_undo_history(profile: Profile) -> QuerySet[UndoAction]:
    """Return this profile's active (non-expired) undo entries, newest first."""
    return UndoAction.objects.for_profile(profile).active()
