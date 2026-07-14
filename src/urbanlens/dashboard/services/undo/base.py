"""Base class and registry for per-model undo-delete handlers.

See the modules under ``services.undo.handlers`` for the concrete, per-model
serialize/restore logic. Importing ``services.undo.handlers`` (done once by
``services.undo.service``) populates the registry below.
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar


class UndoHandler(abc.ABC):
    """Serializes/restores instances of one model for the undo-delete framework.

    Cascade-deleted children (comments, notes, contacts, markup annotations,
    etc.) are gone the instant the parent is deleted - before ``serialize``
    gets a chance to capture them - so ``restore`` only brings back each
    instance's own core fields plus whichever relations are cheap and safe
    to relink (self-referential hierarchy, labels, membership rosters).
    Callers must surface this scope limit to the user before they confirm
    the delete.
    """

    model_label: ClassVar[str]

    @classmethod
    @abc.abstractmethod
    def serialize(cls, instances: list[Any]) -> list[dict[str, Any]]:
        """Capture a JSON-safe snapshot of ``instances``. Call before deleting them."""

    @classmethod
    @abc.abstractmethod
    def describe(cls, instances: list[Any]) -> str:
        """Return a short human-readable label for the undo history list."""

    @classmethod
    @abc.abstractmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Any]:
        """Recreate instances from a payload previously returned by ``serialize``."""


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
