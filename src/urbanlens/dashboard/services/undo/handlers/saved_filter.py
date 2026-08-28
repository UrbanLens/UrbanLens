"""Undo handler for SavedFilter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

_RESTORABLE_FIELDS = ("name", "icon", "criteria", "order", "color", "opacity")

#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.saved_filter import MODEL_LABEL``)
#: instead of hand-typing ``"saved_filter"`` - a typo in a hand-typed string
#: only fails at runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "saved_filter"


@register
class SavedFilterUndoHandler(UndoHandler):
    """Restores a saved filter's name, icon, criteria, and sidebar order."""

    model_label = MODEL_LABEL
    model = SavedFilter

    @classmethod
    def serialize(cls, instances: Sequence[SavedFilter]) -> list[dict[str, Any]]:
        return [cls._serialize_one(saved_filter) for saved_filter in instances]

    @classmethod
    def _serialize_one(cls, saved_filter: SavedFilter) -> dict[str, Any]:
        fields = {name: getattr(saved_filter, name) for name in _RESTORABLE_FIELDS}
        return {"fields": fields, "profile_id": saved_filter.profile_id}

    @classmethod
    def describe(cls, instances: Sequence[SavedFilter]) -> str:
        return describe_batch("Saved filter", "saved filters", [f.name for f in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[SavedFilter]:
        """Recreate the saved filters.

        Raises:
            UndoExpiredError: If the owning profile was deleted during the retention
                window, or the filter's name has since been used for another of that
                profile's filters - ``uq_saved_filter_profile_name`` would otherwise
                surface as an uncaught IntegrityError, the same contract
                ``PinUndoHandler.restore`` follows.
        """
        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        for entry in payload:
            if not Profile.objects.filter(pk=entry["profile_id"]).exists():
                raise UndoExpiredError("The profile that owned this saved filter no longer exists.")
            name = entry["fields"].get("name")
            if name and SavedFilter.objects.filter(profile_id=entry["profile_id"], name=name).exists():
                raise UndoExpiredError(f"You already have a saved filter called \u201c{name}\u201d, so this one can't be restored alongside it.")

        return [SavedFilter.objects.create(profile_id=entry["profile_id"], **entry["fields"]) for entry in payload]
