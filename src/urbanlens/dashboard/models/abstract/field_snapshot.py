"""Scope a partial edit's write to the columns it actually changed."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.db.models import Model


class FieldSnapshot:
    """The concrete column values of a loaded record, for saving only what an edit changed.

    A handler that loads a row, applies some request fields and calls a bare ``save()`` writes every
    column back as it was loaded, reverting whatever another request wrote in between.

    Args:
        instance: The record about to be edited.
    """

    def __init__(self, instance: Model) -> None:
        self.instance = instance
        self._before = {field.attname: copy.deepcopy(getattr(instance, field.attname)) for field in self._fields()}

    def _fields(self) -> list[Any]:
        return [field for field in self.instance._meta.concrete_fields if not field.primary_key]  # noqa: SLF001

    def changed(self) -> list[str]:
        """Names of the columns whose value differs from the snapshot.

        Returns:
            Field names, in model declaration order.
        """
        return [field.name for field in self._fields() if getattr(self.instance, field.attname) != self._before[field.attname]]

    def save_changes(self) -> list[str]:
        """Write only the changed columns, plus ``updated`` when the model has one; write nothing if none changed.

        Returns:
            The changed field names.
        """
        changed = self.changed()
        if changed:
            has_updated = any(field.name == "updated" for field in self._fields())
            self.instance.save(update_fields=[*changed, "updated"] if has_updated and "updated" not in changed else changed)
        return changed
