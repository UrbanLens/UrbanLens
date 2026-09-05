"""Delete a stored file when the row that named it stops naming it.

Django stopped removing `FileField` files on delete in 1.3, deliberately - a
rolled-back transaction would otherwise leave a row pointing at a file that no
longer exists. Nothing has removed them since, so two ordinary actions strand a
file on disk:

* **Replacing** an icon or avatar. The new upload is written, the column is
  repointed, and the previous file stays forever.
* **Deleting** the row. The pin, label or achievement goes; its icon does not.

Individually small - these are decorative images - but they accumulate with
normal use, and a stranded file is exactly the "orphan" case
`services/media/access.py` had to start refusing to serve, because an orphan is
indistinguishable from a live file whose owner the viewer may not learn about.

**Why receivers rather than per-caller deletes.** The clears already delete
their file (`controllers/labels.py`, `controllers/achievements.py`), and doing
the same at every replace and delete site means finding all of them and every
future one. There are five fields across four models and the write paths include
forms, the external API, import, and the admin. One rule in one place is the
shape that cannot be forgotten.

**Ordering, which is the part that has to be right.** Deletion happens on
`post_delete` and after a successful `save()`, never before - so a failed write
or a rolled-back transaction cannot leave a row pointing at a file that is gone.
The cost is the opposite failure: a transaction that commits the row and then
rolls back for another reason leaves the file deleted. That is the direction the
data survives, which is the one to prefer.

Registered in `dashboard/apps.py` with `dispatch_uid`, per this repository's
signal convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

if TYPE_CHECKING:
    from django.db.models import Model

logger = logging.getLogger(__name__)

#: ``(app label, model name, field name)`` for every file this manages.
#:
#: Deliberately not "every FileField": `Image`'s four columns are handled by
#: that model's own delete paths, which understand when two rows legitimately
#: share a file (`attach_existing_comment_image` copies rather than sharing, and
#: the bulk deletes carry a shared-reference rule). Adding them here would delete
#: a file another row still points at.
MANAGED_FILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "Pin", "custom_icon"),
    ("dashboard", "Label", "custom_icon"),
    ("dashboard", "Achievement", "custom_icon"),
    ("dashboard", "Profile", "avatar"),
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
    try:
        getattr(instance, field).storage.delete(name)
    except OSError:
        # A missing file is the normal case on a re-run, and a storage backend
        # that is unavailable is not a reason to fail the write that triggered
        # this. Logged rather than swallowed silently.
        logger.warning("Could not delete replaced file %s for %s %s", name, instance._meta.object_name, instance.pk, exc_info=True)  # noqa: SLF001


@receiver(pre_save, dispatch_uid="media_file_cleanup_remember_replaced")
def _remember_replaced_file(sender, instance, **kwargs) -> None:
    """Note the file a save is about to replace, before the column changes.

    Read here and deleted in `post_save`: the stored name is only knowable
    before the write, and deleting it is only safe after the write succeeds.
    """
    fields = _fields_for(instance)
    if not fields or instance.pk is None:
        return

    update_fields = kwargs.get("update_fields")
    replaced: list[tuple[str, str]] = []
    stored = sender.objects.filter(pk=instance.pk).values(*fields).first()
    if stored is None:
        return
    for field in fields:
        if update_fields is not None and field not in update_fields:
            continue
        previous = stored[field]
        if previous and previous != getattr(instance, field).name:
            replaced.append((field, previous))
    if replaced:
        setattr(instance, _REPLACED, replaced)


@receiver(post_save, dispatch_uid="media_file_cleanup_delete_replaced")
def _delete_replaced_file(sender, instance, **kwargs) -> None:
    """Delete what the completed save replaced."""
    for field, name in getattr(instance, _REPLACED, ()):
        _discard(instance, field, name)
    if hasattr(instance, _REPLACED):
        delattr(instance, _REPLACED)


@receiver(post_delete, dispatch_uid="media_file_cleanup_delete_removed")
def _delete_removed_file(sender, instance, **kwargs) -> None:
    """Delete the files of a row that has been removed."""
    for field in _fields_for(instance):
        stored = getattr(instance, field)
        if stored and stored.name:
            _discard(instance, field, stored.name)
