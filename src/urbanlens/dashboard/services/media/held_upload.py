"""An uploaded icon or avatar is held where nothing serves it until the sandbox worker has re-encoded it (P119).

The upload is stored under :data:`HELD_PREFIX`, which has no media authorizer, so the gate serves it to nobody, and its
name is kept in the row's ``<field>_upload`` column. The worker writes the re-encoded file into the field itself, so the
field only ever names a file this server encoded, and nothing that shows it needs to know an upload is waiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import io
import logging
from typing import TYPE_CHECKING, Any
import uuid

from django.apps import apps
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL.Image import DecompressionBombError

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.core.files import File
    from django.core.files.storage import Storage
    from django.db.models import Model

logger = logging.getLogger(__name__)

#: Where held uploads are stored.
HELD_PREFIX = "unprocessed"

#: The longest side a stored icon keeps.
ICON_MAX_PX = 256

#: The longest side a stored avatar keeps.
AVATAR_MAX_PX = 512

#: How long a held upload waits before the sweep takes it that its publish was never queued.
STALLED_HELD_AGE = timedelta(minutes=15)

#: How many publishes of one held upload may start without finishing before the sweep drops it. Counted when a publish
#: starts rather than when it is queued, so a queue backed up behind someone else's import costs nobody their upload.
MAX_HELD_STARTS = 3


def _starts_key(held_name: str) -> str:
    return f"held-upload-starts:{held_name}"


def _touch_label_pins(label_id: int) -> None:
    from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_labels

    touch_pins_for_labels([label_id])


def _touch_pin(pin_id: int) -> None:
    from urbanlens.dashboard.services.map_pins.touch import touch_pin

    touch_pin(pin_id)


@dataclass(frozen=True)
class HeldField:
    """A file field whose uploads are held until they are re-encoded."""

    model: str
    field: str
    max_dimension: int
    #: Label and pin icons keep the file they replace, because undo restores a deleted row by its stored name.
    delete_replaced: bool
    #: Clients that cache a label by ``updated`` redraw its icon when it moves.
    bump_updated: bool = False
    after_publish: Callable[[int], None] | None = None

    @property
    def key(self) -> str:
        """The name a queued task refers to this field by."""
        return f"{self.model}.{self.field}"

    @property
    def upload_column(self) -> str:
        """The column naming a held upload."""
        return f"{self.field}_upload"

    @property
    def storage(self) -> Storage:
        """The storage the field writes to."""
        return apps.get_model(self.model)._meta.get_field(self.field).storage  # noqa: SLF001 - _meta is Django's public model API


HELD_FIELDS: dict[str, HeldField] = {
    held.key: held
    for held in (
        HeldField("dashboard.Label", "custom_icon", ICON_MAX_PX, delete_replaced=False, bump_updated=True, after_publish=_touch_label_pins),
        HeldField("dashboard.Pin", "custom_icon", ICON_MAX_PX, delete_replaced=False, after_publish=_touch_pin),
        HeldField("dashboard.Achievement", "custom_icon", ICON_MAX_PX, delete_replaced=True),
        HeldField("dashboard.Profile", "avatar", AVATAR_MAX_PX, delete_replaced=True),
    )
}


def held_field(instance: Model, field: str) -> HeldField:
    """The :class:`HeldField` for *instance*'s *field*.

    Raises:
        KeyError: The field does not hold its uploads.
    """
    return HELD_FIELDS[f"{instance._meta.label}.{field}"]  # noqa: SLF001 - _meta is Django's public model API


def _delete_quietly(storage: Storage, name: str) -> None:
    try:
        storage.delete(name)
    except OSError:
        logger.warning("Could not delete held upload %s", name, exc_info=True)


def hold_upload(instance: Model, field: str, upload: File) -> str:
    """Store *upload* where nothing serves it and name it on *instance*, which the caller then saves.

    Any upload *instance* already held is deleted once this transaction commits.

    Args:
        instance: The row the upload is for.
        field: Its file field.
        upload: The uploaded or downloaded file, already size-checked, sniffed and scanned.

    Returns:
        The column the caller must save.
    """
    held = held_field(instance, field)
    # No extension and no uploaded name: the worker decodes by content, and a name can say as much as metadata.
    name = held.storage.save(f"{HELD_PREFIX}/{uuid.uuid4().hex}", upload)
    discard_held_upload(instance, field)
    setattr(instance, held.upload_column, name)
    return held.upload_column


def discard_held_upload(instance: Model, field: str) -> str:
    """Forget the upload *instance* holds for *field*, deleting it once this transaction commits.

    Args:
        instance: The row.
        field: Its file field.

    Returns:
        The column the caller must save.
    """
    held = held_field(instance, field)
    if previous := getattr(instance, held.upload_column):
        storage = held.storage
        transaction.on_commit(lambda: _delete_quietly(storage, previous))
        setattr(instance, held.upload_column, "")
    return held.upload_column


def queue_held_upload(instance: Model, field: str) -> None:
    """Ask the sandbox worker to publish the upload *instance* holds for *field*, once this transaction commits.

    Args:
        instance: A saved row.
        field: Its file field.
    """
    held = held_field(instance, field)
    name = getattr(instance, held.upload_column)
    if not name:
        return
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import publish_held_upload

    key, pk = held.key, instance.pk
    transaction.on_commit(lambda: safely_enqueue_task(publish_held_upload, key, pk, name))


def publish_held(key: str, pk: int, held_name: str) -> bool:
    """Write a re-encoded copy of a held upload into its field, if the row still holds that upload.

    One that cannot be decoded is dropped, and the field keeps what it showed.

    Args:
        key: The :attr:`HeldField.key`.
        pk: The row.
        held_name: The held upload's stored name.

    Returns:
        Whether the field now shows the re-encoded upload.

    Raises:
        OSError: Storage could not hand back the held file.
    """
    from urbanlens.dashboard.services.media.images import reencode_image_file

    held = HELD_FIELDS[key]
    rows = apps.get_model(held.model).objects.all()
    holding = {"pk": pk, held.upload_column: held_name}
    row = rows.filter(**holding).first()
    if row is None:
        return False
    shown = getattr(row, held.field)
    storage = shown.storage
    with storage.open(held_name, "rb") as handle:
        raw = handle.read()
    from django.core.cache import cache

    cache.set(_starts_key(held_name), cache.get(_starts_key(held_name), 0) + 1, timeout=int(timedelta(days=2).total_seconds()))
    try:
        data, extension = reencode_image_file(io.BytesIO(raw), max_dimension=held.max_dimension, convert_webp=True)
    except (OSError, ValueError, EOFError, SyntaxError, DecompressionBombError) as exc:
        logger.warning("Dropping the upload held for %s %s: %s", key, pk, exc)
        drop_held(key, pk, held_name)
        return False

    new_name = storage.save(shown.field.generate_filename(row, f"{uuid.uuid4().hex}{extension}"), ContentFile(data))
    also_set: dict[str, Any] = {held.field: new_name, held.upload_column: ""}
    if held.bump_updated:
        also_set["updated"] = timezone.now()
    with transaction.atomic():
        previous = list(rows.select_for_update().filter(**holding).values_list(held.field, flat=True))
        replaced = bool(previous) and bool(rows.filter(**holding).update(**also_set))
    if not replaced:
        _delete_quietly(storage, new_name)
        return False
    _delete_quietly(storage, held_name)
    if held.delete_replaced and previous[0]:
        _delete_quietly(storage, previous[0])
    if held.after_publish is not None:
        held.after_publish(pk)
    return True


def drop_held(key: str, pk: int, held_name: str) -> bool:
    """Forget a held upload that will never be published, if the row still holds it, and delete the file.

    Args:
        key: The :attr:`HeldField.key`.
        pk: The row.
        held_name: The held upload's stored name.

    Returns:
        Whether the row held it.
    """
    held = HELD_FIELDS[key]
    rows = apps.get_model(held.model).objects.filter(pk=pk, **{held.upload_column: held_name})
    if not rows.update(**{held.upload_column: ""}):
        return False
    _delete_quietly(held.storage, held_name)
    return True


def reencode_shown(key: str, pk: int, name: str) -> bool:
    """Re-encode a file the field already shows, stored before uploads to it were held. One that cannot be decoded is removed.

    Args:
        key: The :attr:`HeldField.key`.
        pk: The row.
        name: The stored name the field shows.

    Returns:
        Whether the file was replaced or removed.

    Raises:
        OSError: Storage could not hand back the file.
    """
    from urbanlens.dashboard.services.media.stored_field import Reencoded, clear_stored_field, reencode_stored_field

    held = HELD_FIELDS[key]
    rows = apps.get_model(held.model).objects.all()
    also_set = {"updated": timezone.now()} if held.bump_updated else None
    outcome = reencode_stored_field(rows, pk, held.field, name, max_dimension=held.max_dimension, convert_webp=True, also_set=also_set)
    changed = outcome is Reencoded.REPLACED or (outcome is Reencoded.UNDECODABLE and clear_stored_field(rows, pk, held.field, name, also_set=also_set))
    if changed and held.after_publish is not None:
        held.after_publish(pk)
    return changed


def sweep_held_uploads() -> tuple[int, int]:
    """Queue held uploads whose publish never ran, drop ones queued too often, and remove held files nothing names.

    A file whose row was deleted is kept past the undo window, because undo restores the row with the held name.

    Returns:
        How many held uploads were queued or dropped, and how many unnamed files were removed.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.undo.model import UNDO_RETENTION
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import publish_held_upload

    now = timezone.now()
    handled = 0
    named: set[str] = set()
    storages: dict[int, Storage] = {}
    for held in HELD_FIELDS.values():
        storage = storages.setdefault(id(held.storage), held.storage)
        rows = apps.get_model(held.model).objects.exclude(**{held.upload_column: ""}).order_by().values_list("pk", held.upload_column)
        for pk, name in rows.iterator():
            named.add(name)
            try:
                if not storage.exists(name):
                    handled += drop_held(held.key, pk, name)
                    continue
                stalled = now - storage.get_modified_time(name) >= STALLED_HELD_AGE
            except OSError:
                logger.warning("Could not check the upload held for %s %s", held.key, pk, exc_info=True)
                continue
            if not stalled:
                continue
            starts = cache.get(_starts_key(name), 0)
            if starts >= MAX_HELD_STARTS:
                logger.warning("Dropping the upload held for %s %s: its publish started %s times and never finished", held.key, pk, starts)
                drop_held(held.key, pk, name)
            else:
                safely_enqueue_task(publish_held_upload, held.key, pk, name)
            handled += 1

    removed = 0
    for storage in storages.values():
        try:
            _directories, files = storage.listdir(HELD_PREFIX)
        except OSError:
            continue
        for file in files:
            name = f"{HELD_PREFIX}/{file}"
            if name in named:
                continue
            try:
                age = now - storage.get_modified_time(name)
            except OSError:
                continue
            if age >= UNDO_RETENTION + timedelta(days=1):
                _delete_quietly(storage, name)
                removed += 1
    return handled, removed
