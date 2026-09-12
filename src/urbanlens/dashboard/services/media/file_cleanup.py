"""Delete a stored file when the row that named it stops naming it.
Django stopped removing `FileField` files on delete in 1.3, deliberately - a rolled-back transaction would otherwise leave a row pointing at a file that no longer exists."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save

if TYPE_CHECKING:
    from django.db.models import Model

logger = logging.getLogger(__name__)

#: ``(app label, model name, field name)`` for every file this manages.
#: Deliberately not "every FileField".
MANAGED_FILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "Achievement", "custom_icon"),
    ("dashboard", "Profile", "avatar"),
)

#: Deliberately absent: `Pin.custom_icon` and `Label.custom_icon`.
#: Both models are restorable by the undo framework, which stashes the icon as its stored *name*
#: rather than its bytes (`services/undo/handlers/pin.py`, `.../label.py`).
UNDO_RESTORABLE_FILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "Pin", "custom_icon"),
    ("dashboard", "Label", "custom_icon"),
)

#: Set on an instance by `_remember_replaced_file` for `_delete_replaced_file`.
_REPLACED = "_ul_replaced_files"


def _fields_for(instance: Model) -> tuple[str, ...]:
    """The managed field names on `instance`'s model, if any.

    Args:
        instance: The model instance being saved or deleted.

    Returns:
        The field names this module manages for that model.
    """
    meta = instance._meta  # noqa: SLF001 - the documented way to read a model's own labels
    return tuple(field for app, model, field in MANAGED_FILE_FIELDS if app == meta.app_label and model == meta.object_name)


def _discard(instance: Model, field: str, name: str) -> None:
    """Remove one stored file, never raising into the caller's write.

    Args:
        instance: The row the file belonged to, for the log line.
        field: The field that named it.
        name: The stored file name.
    """
    storage = getattr(instance, field).storage

    def unlink() -> None:
        _unlink(storage, name, instance, field)

    # Only once the write is real. `post_save`/`post_delete` run inside the
    # transaction, and this codebase deletes these rows inside `atomic()`
    # blocks, so unlinking here would survive a rollback that restored the row.
    transaction.on_commit(unlink)


def _unlink(storage, name: str, instance: Model, field: str) -> None:
    """Remove one file from storage, never raising into the caller.

    Args:
        storage: The field's storage backend.
        name: The stored file name.
        instance: The row it belonged to, for the log line.
        field: The field that named it.
    """
    try:
        storage.delete(name)
    except OSError:
        # A missing file is the normal case on a re-run, and a storage backend
        # that is unavailable is not a reason to fail the write that triggered
        # this. Logged rather than swallowed silently.
        logger.warning("Could not delete replaced file %s for %s %s", name, instance._meta.object_name, instance.pk, exc_info=True)  # noqa: SLF001


def remember_replaced_file(sender, instance, **kwargs) -> None:
    """Note the file a save is about to replace, before the column changes.
    Read here and deleted in `post_save`: the stored name is only knowable before the write, and deleting it is only safe after the write succeeds."""
    fields = _fields_for(instance)
    if not fields or instance.pk is None:
        return

    # Narrowed before the query, not after: a save naming `update_fields`
    # without a managed column cannot replace anything, and should not pay for a
    # round trip to find that out.
    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        fields = tuple(field for field in fields if field in update_fields)
    if not fields:
        return

    replaced: list[tuple[str, str]] = []
    stored = sender.objects.filter(pk=instance.pk).values(*fields).first()
    if stored is not None:
        for field in fields:
            previous = stored[field]
            if previous and previous != getattr(instance, field).name:
                replaced.append((field, previous))
    # Assigned unconditionally. Set only when non-empty, a save that raised
    # after this ran would leave its list on the instance for the *next* save to
    # act on - deleting a file that save never touched.
    setattr(instance, _REPLACED, replaced)


def delete_replaced_file(sender, instance, **kwargs) -> None:
    """Delete what the completed save replaced."""
    for field, name in getattr(instance, _REPLACED, ()):
        _discard(instance, field, name)
    if hasattr(instance, _REPLACED):
        delattr(instance, _REPLACED)


def delete_removed_file(sender, instance, **kwargs) -> None:
    """Delete the files of a row that has been removed."""
    for field in _fields_for(instance):
        stored = getattr(instance, field)
        if stored and stored.name:
            _discard(instance, field, stored.name)


def connect() -> None:
    """Wire the receivers to the four models that have a managed file.
    Per sender rather than globally: see the module docstring."""
    from django.apps import apps as django_apps

    for app_label, model_name, _field in MANAGED_FILE_FIELDS:
        model = django_apps.get_model(app_label, model_name)
        pre_save.connect(remember_replaced_file, sender=model, dispatch_uid=f"media_file_cleanup_remember_{model_name}")
        post_save.connect(delete_replaced_file, sender=model, dispatch_uid=f"media_file_cleanup_replaced_{model_name}")
        post_delete.connect(delete_removed_file, sender=model, dispatch_uid=f"media_file_cleanup_removed_{model_name}")
