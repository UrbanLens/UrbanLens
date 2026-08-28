"""Base class and registry for per-model undo handlers.

See the modules under ``services.undo.handlers`` for the concrete, per-model
serialize/restore (deletes) and undo/redo mutation logic. Importing
``services.undo.handlers`` (done once by ``services.undo.service``) populates
the registry below.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import Model


class UndoHandler(abc.ABC):
    """Serializes/restores instances of one model for the undo framework.

    Delete handlers (``supports_delete`` True, the default) capture a snapshot
    before the row is removed and recreate it on undo. Mutation handlers
    (``MutationUndoHandler``) record a reversible change instead.

    Cascade-deleted children (comments, notes, contacts, markup annotations,
    etc.) are gone the instant the parent is deleted - before ``serialize``
    gets a chance to capture them - so ``restore`` only brings back each
    instance's own core fields plus whichever relations are cheap and safe
    to relink (self-referential hierarchy, labels, membership rosters).
    Callers must surface this scope limit to the user before they confirm
    the delete.
    """

    model_label: ClassVar[str]
    #: The Django model this delete handler recreates. Used to re-delete the
    #: restored rows on redo. Mutation handlers leave this None.
    model: ClassVar[type[Model] | None] = None
    supports_delete: ClassVar[bool] = True

    @classmethod
    @abc.abstractmethod
    def serialize(cls, instances: Sequence[Any]) -> list[dict[str, Any]]:
        """Capture a JSON-safe snapshot of ``instances``. Call before deleting them."""

    @classmethod
    @abc.abstractmethod
    def describe(cls, instances: Sequence[Any]) -> str:
        """Return a short human-readable label for the undo history list."""

    @classmethod
    @abc.abstractmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Any]:
        """Recreate instances from a payload previously returned by ``serialize``."""

    @classmethod
    def redo_delete(cls, payload: dict[str, Any]) -> None:
        """Re-delete rows that ``restore`` just recreated (the redo of a delete-undo).

        Args:
            payload: Wrapped stash of the form ``{"entries": ..., "restored_pks": [...]}``.
        """
        pks = payload.get("restored_pks") or []
        if cls.model is None or not pks:
            return
        cls.model.objects.filter(pk__in=pks).delete()

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any]) -> None:  # noqa: ARG003 - interface; override uses payload
        """Apply the inverse of a stashed mutation.

        Args:
            payload: The dict previously given to ``stash_mutation``.
        """
        raise TypeError(f"{cls.model_label} does not support mutations.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any]) -> None:  # noqa: ARG003 - interface; override uses payload
        """Re-apply a stashed mutation after it was undone.

        Args:
            payload: The dict previously given to ``stash_mutation``.
        """
        raise TypeError(f"{cls.model_label} does not support mutations.")


class MutationUndoHandler(UndoHandler):
    """Undo handler for a reversible change rather than a deletion.

    ``serialize``/``restore`` are not used. The payload is a dict describing
    the change, applied by ``undo_mutation`` / ``redo_mutation``.
    """

    supports_delete = False

    @classmethod
    def serialize(cls, instances: Sequence[Any]) -> list[dict[str, Any]]:  # noqa: ARG003 - mutations are not serialized
        raise TypeError(f"{cls.model_label} records mutations, not deletions.")

    @classmethod
    def describe(cls, instances: Sequence[Any]) -> str:  # noqa: ARG003 - mutations are not serialized
        raise TypeError(f"{cls.model_label} records mutations, not deletions.")

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Any]:  # noqa: ARG003 - mutations are not serialized
        raise TypeError(f"{cls.model_label} records mutations, not deletions.")


_HANDLERS: dict[str, type[UndoHandler]] = {}


def register(handler: type[UndoHandler]) -> type[UndoHandler]:
    """Class decorator: register a handler under its ``model_label``."""
    _HANDLERS[handler.model_label] = handler
    return handler


def get_handler(model_label: str) -> type[UndoHandler]:
    """Look up a registered handler by its ``model_label``.

    Raises:
        ValueError: If no handler is registered under that label.
    """
    try:
        return _HANDLERS[model_label]
    except KeyError:
        raise ValueError(f"No undo handler registered for {model_label!r}") from None


def describe_batch(singular_label: str, plural_label: str, names: list[str], max_shown: int = 3) -> str:
    """Build a ``describe()`` string that names names instead of just a bare count.

    Args:
        singular_label: Label for a single instance, e.g. ``"Pin"``.
        plural_label: Label for the plural count, e.g. ``"pins"``.
        names: Display name of every instance in the batch, in order.
        max_shown: Maximum number of names to list before collapsing the rest
            into a "(+N more)" suffix.

    Returns:
        e.g. ``"Pin: Old Mill"``, or ``"5 pins: Old Mill, Grain Silo, Water Tower (+2 more)"``.
    """
    if len(names) == 1:
        return f"{singular_label}: {names[0]}"
    shown = ", ".join(names[:max_shown])
    remaining = len(names) - max_shown
    suffix = f" (+{remaining} more)" if remaining > 0 else ""
    return f"{len(names)} {plural_label}: {shown}{suffix}"
