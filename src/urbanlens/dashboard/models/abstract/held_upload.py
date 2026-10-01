from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from django.db import models as django_models

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from django.db.models.fetch_modes import FetchMode


class HeldUploadModel(django_models.Model):
    """A model whose file uploads the sandbox worker publishes after the row was read (see ``services.media.held_upload``).

    A full ``save()`` of a row read before the publish would write back the file names it read, so a full save leaves
    those columns out unless the instance changed them.
    """

    class Meta:
        abstract = True

    def _held_columns(self) -> tuple[str, ...]:
        from urbanlens.dashboard.services.media.held_upload import HELD_FIELDS

        label = self._meta.label
        return tuple(column for held in HELD_FIELDS.values() if held.model == label for column in (held.field, held.upload_column))

    def _held_value(self, column: str) -> str:
        return str(getattr(self, column) or "")

    def _record_held(self, columns: Iterable[str]) -> None:
        loaded: dict[str, str] = self.__dict__.setdefault("_held_loaded", {})
        for column in columns:
            if column in self.__dict__:
                loaded[column] = self._held_value(column)

    @classmethod
    def from_db(cls, db: str | None, field_names: Collection[str], values: Collection[Any], *, fetch_mode: FetchMode | None = None) -> Self:  # noqa: ARG003
        instance = super().from_db(db, field_names, values)
        instance._record_held(instance._held_columns())  # noqa: SLF001
        return instance

    def refresh_from_db(self, *args: Any, **kwargs: Any) -> None:
        super().refresh_from_db(*args, **kwargs)
        # A deferred field's first read refreshes only that field; a held column this instance changed is not reloaded.
        fields = kwargs.get("fields", args[1] if len(args) > 1 else None)
        self._record_held(self._held_columns() if fields is None else set(self._held_columns()).intersection(fields))

    def save(self, *args: Any, **kwargs: Any) -> None:
        loaded: dict[str, str] = self.__dict__.get("_held_loaded", {})
        if loaded and not args and not self._state.adding and self.pk is not None and kwargs.get("update_fields") is None and not kwargs.get("force_insert"):
            unchanged = {column for column, value in loaded.items() if self._held_value(column) == value}
            if unchanged:
                kwargs["update_fields"] = [field.name for field in self._meta.concrete_fields if not field.primary_key and not field.generated and field.name not in unchanged and field.attname in self.__dict__]
        super().save(*args, **kwargs)
        written = kwargs.get("update_fields")
        self._record_held(self._held_columns() if written is None else set(self._held_columns()).intersection(written))
