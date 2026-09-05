"""Delete a stored file when the row that named it stops naming it.

Django stopped removing `FileField` files on delete in 1.3, deliberately - a
rolled-back transaction would otherwise leave a row pointing at a file that no
longer exists. Nothing has removed them since, so two ordinary actions strand a
file on disk:

* **Replacing** an icon or avatar. The new upload is written, the column is
  repointed, and the previous file stays forever.
* **Deleting** the row. The achievement or profile goes; its icon does not.

Individually small - these are decorative images - but they accumulate with
normal use, and a stranded file is exactly the "orphan" case
`services/media/access.py` had to start refusing to serve, because an orphan is
indistinguishable from a live file whose owner the viewer may not learn about.

**Why receivers rather than per-caller deletes.** The clears already delete
their file (`controllers/labels.py`, `controllers/achievements.py`), and doing
the same at every replace and delete site means finding all of them and every
future one. The write paths include the profile form, the achievement admin and the
external API's avatar routes, and each replace and delete site among them would
otherwise have to remember. One rule in one place is the shape that cannot be
forgotten.

**Ordering, which is the part that has to be right.** `post_save` and
`post_delete` fire *inside* the transaction, before commit - so deleting there
would unlink the file and then let a rollback put the row back, pointing at
nothing. Every unlink is therefore deferred with `transaction.on_commit`, which
runs it only if the write actually lands (and immediately when there is no
transaction). That is the direction in which the data survives: the failure it
leaves possible is a file nobody points at, not a row pointing at a file that is
gone.

**Connected per sender**, in `connect()` below. A sender-less `@receiver` makes
*every* model in the project report listeners, which disables Django's
fast-delete path repo-wide and trips
`test_bulk_write_signal_guard` - that test asks "which models have receivers a
bulk write would skip", and the answer became "all of them".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save

if TYPE_CHECKING:
    from django.db.models import Model

logger = logging.getLogger(__name__)

#: ``(app label, model name, field name)`` for every file this manages.
#:
#: Deliberately not "every FileField". `Image`'s columns belong to that model's
#: own delete paths, which understand when two rows legitimately share a file
#: (`services/media/images.py`'s `delete_stored_file` checks
#: `file_still_referenced` for `image`, `thumbnail` and `marker_thumbnail`);
#: adding them here would delete a file another row still points at. Note that
#: `analysis_thumbnail` is covered by neither - see P14.
MANAGED_FILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "Achievement", "custom_icon"),
    ("dashboard", "Profile", "avatar"),
)

#: Deliberately absent: `Pin.custom_icon` and `Label.custom_icon`.
#:
#: Both models are restorable by the undo framework, which stashes the icon as
#: its stored *name* rather than its bytes
#: (`services/undo/handlers/pin.py`, `.../label.py`). Deleting the file on
#: delete - or on replace - would leave an undo within the window restoring a
#: row that names a file no longer there, and it would do so silently: a broken
#: icon with nothing to explain it, which is worse than the stranded file this
#: module exists to stop.
#:
#: They want deletion deferred to when the `UndoAction` is pruned, so the file
#: outlives the row for exactly as long as the row can come back. That is a
#: different mechanism from these receivers, not a longer list - see P14.
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

    Read here and deleted in `post_save`: the stored name is only knowable
    before the write, and deleting it is only safe after the write succeeds.
    """
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

    Per sender rather than globally: see the module docstring.
    """
    from django.apps import apps as django_apps

    for app_label, model_name, _field in MANAGED_FILE_FIELDS:
        model = django_apps.get_model(app_label, model_name)
        pre_save.connect(remember_replaced_file, sender=model, dispatch_uid=f"media_file_cleanup_remember_{model_name}")
        post_save.connect(delete_replaced_file, sender=model, dispatch_uid=f"media_file_cleanup_replaced_{model_name}")
        post_delete.connect(delete_removed_file, sender=model, dispatch_uid=f"media_file_cleanup_removed_{model_name}")
