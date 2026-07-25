"""Undo handler for SavedFilter."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

_RESTORABLE_FIELDS = ("name", "icon", "criteria", "order")

#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.saved_filter import MODEL_LABEL``)
#: instead of hand-typing ``"saved_filter"`` - a typo in a hand-typed string
#: only fails at runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "saved_filter"


@register
class SavedFilterUndoHandler(UndoHandler):
    """Restores a saved filter's name, icon, criteria, and sidebar order."""

    model_label = MODEL_LABEL

    @classmethod
    def serialize(cls, instances: list[SavedFilter]) -> list[dict[str, Any]]:
        return [cls._serialize_one(saved_filter) for saved_filter in instances]

    @classmethod
    def _serialize_one(cls, saved_filter: SavedFilter) -> dict[str, Any]:
        fields = {name: getattr(saved_filter, name) for name in _RESTORABLE_FIELDS}
        return {"fields": fields, "profile_id": saved_filter.profile_id}

    @classmethod
    def describe(cls, instances: list[SavedFilter]) -> str:
        return describe_batch("Saved filter", "saved filters", [f.name for f in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[SavedFilter]:
        return [SavedFilter.objects.create(profile_id=entry["profile_id"], **entry["fields"]) for entry in payload]
