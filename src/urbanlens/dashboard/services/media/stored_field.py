"""Re-encode an image held in a model's file field: a comment image, an icon or an avatar.

The swap is a conditional update on the stored name, so a row edited or deleted meanwhile keeps what it has, and
whichever file lost is deleted, or swept once no row names it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import enum
import io
import logging
from typing import TYPE_CHECKING, Any
import uuid

from django.core.files.base import ContentFile
from PIL.Image import DecompressionBombError

from urbanlens.dashboard.services.media.images import reencode_image_file
from urbanlens.dashboard.services.media.storage_errors import STORAGE_ERRORS
from urbanlens.dashboard.services.security.redact import redact_filename

if TYPE_CHECKING:
    from django.core.files.storage import Storage
    from django.db.models import Model, QuerySet

logger = logging.getLogger(__name__)

#: The fields whose directories :func:`sweep_unnamed_files` sweeps: the media gate serves any icon or avatar path, and a
#: comment image's upload as sent carries whatever metadata it came with.
SWEPT_FIELDS = (
    "dashboard.Label.custom_icon",
    "dashboard.Pin.custom_icon",
    "dashboard.Achievement.custom_icon",
    "dashboard.Profile.avatar",
    "dashboard.Comment.image",
    "dashboard.TripComment.image",
)


def delete_unnamed_file(storage: Storage, name: str) -> bool:
    """Delete a file its row no longer names, leaving it to :func:`sweep_unnamed_files` when storage refuses.

    A refused delete does not undo or fail what made the row stop naming the file.

    Args:
        storage: The field's storage.
        name: The stored name.

    Returns:
        Whether it was deleted.
    """
    try:
        storage.delete(name)
    except STORAGE_ERRORS:
        logger.warning("Could not delete %s, which no row names; the sweep will try again", redact_filename(name), exc_info=True)
        return False
    return True


def sweep_unnamed_files() -> int:
    """Delete the files in each :data:`SWEPT_FIELDS` directory that no file field storing there names.

    A file younger than the task hard time limit may be waiting for the row that will name it to commit, and one an
    undo record inside the retention window mentions may be named again by a restore, so both are kept.

    Returns:
        How many files were deleted.

    Raises:
        Exception: Whatever storage raises that is not one of
            :data:`~urbanlens.dashboard.services.media.held_upload.STORAGE_ERRORS`, such as missing credentials.
    """
    from django.apps import apps
    from django.db.models import FileField

    stored: list[tuple[type[Model], str, Storage, str]] = []
    swept: dict[tuple[int, str], Storage] = {}
    for model in apps.get_models():
        for field in model._meta.get_fields():  # noqa: SLF001 - _meta is Django's public model API
            if isinstance(field, FileField) and isinstance(field.upload_to, str):
                directory = field.upload_to.strip("/")
                stored.append((model, field.name, field.storage, directory))
                if f"{model._meta.label}.{field.name}" in SWEPT_FIELDS:  # noqa: SLF001 - _meta is Django's public model API
                    swept[id(field.storage), directory] = field.storage

    removed = 0
    for (storage_id, directory), storage in swept.items():
        try:
            _directories, files = storage.listdir(directory)
        except FileNotFoundError:
            continue
        except STORAGE_ERRORS:
            logger.warning("Could not list %s to sweep it", directory, exc_info=True)
            continue
        named: set[str] = set()
        for model, name, field_storage, field_directory in stored:
            if (id(field_storage), field_directory) == (storage_id, directory):
                named.update(model._base_manager.exclude(**{name: ""}).filter(**{f"{name}__isnull": False}).values_list(name, flat=True))  # noqa: SLF001 - Django's public model API
        for file in files:
            path = f"{directory}/{file}"
            if path not in named and _may_delete(storage, path):
                removed += delete_unnamed_file(storage, path)
    return removed


def _may_delete(storage: Storage, path: str) -> bool:
    """Whether a file no row names is old enough, and absent from every undo record that could still restore it.

    A younger file may be waiting for the row that will name it to commit.

    Args:
        storage: The file's storage.
        path: Its stored name.

    Returns:
        True when deleting it cannot lose anything a row or an undo will name.
    """
    from django.conf import settings
    from django.utils import timezone

    from urbanlens.dashboard.models.undo.model import UNDO_RETENTION, UndoAction

    now = timezone.now()
    try:
        if now - storage.get_modified_time(path) < timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT):
            return False
    except STORAGE_ERRORS:
        return False
    return not UndoAction.objects.filter(created__gt=now - UNDO_RETENTION, payload__icontains=path).exists()


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What a sweep found."""

    files: int
    bytes: int


def sweep_unnamed_image_files(*, delete: bool) -> SweepReport:
    """Find the files under ``pin_images/`` that none of an Image row's four file columns names.

    Not in :data:`SWEPT_FIELDS`: Image files sit in nested per-upload directories and are named by four columns, so
    this walks the whole tree and reads every row's names, a cost sized to the table rather than to an hourly beat.
    The media gate refuses a file no Image row names, so nothing serves what this removes.

    Args:
        delete: Remove what it finds; otherwise only report it.

    Returns:
        How many unnamed files, and their total size, it found (and removed, when *delete*).
    """
    from urbanlens.dashboard.models.images.model import Image

    columns = ("image", "thumbnail", "marker_thumbnail", "analysis_thumbnail")
    storage = Image._meta.get_field("image").storage  # noqa: SLF001 - _meta is Django's public model API
    named: set[str] = set()
    for row in Image._base_manager.values_list(*columns).iterator(chunk_size=5000):  # noqa: SLF001 - Django's public model API
        named.update(name for name in row if name)

    files = total = 0
    pending = ["pin_images"]
    while pending:
        directory = pending.pop()
        try:
            subdirectories, names = storage.listdir(directory)
        except FileNotFoundError:
            continue
        pending.extend(f"{directory}/{sub}" for sub in subdirectories)
        for name in names:
            path = f"{directory}/{name}"
            if path in named or not _may_delete(storage, path):
                continue
            try:
                size = storage.size(path)
            except STORAGE_ERRORS:
                size = 0
            if delete and not delete_unnamed_file(storage, path):
                continue
            files += 1
            total += size
    return SweepReport(files=files, bytes=total)


class Reencoded(enum.Enum):
    """What :func:`reencode_stored_field` did."""

    REPLACED = "replaced"
    #: The row no longer holds that file, or no longer matches the filter.
    STALE = "stale"
    UNDECODABLE = "undecodable"


def reencode_stored_field(
    rows: QuerySet[Any],
    pk: int,
    field: str,
    stored_name: str,
    *,
    max_dimension: int | None,
    convert_webp: bool,
    only_if: dict[str, Any] | None = None,
    also_set: dict[str, Any] | None = None,
) -> Reencoded:
    """Replace a row's stored image with one re-encoded from its pixels.

    Args:
        rows: The rows the row is found in, e.g. ``Label.objects.all()``.
        pk: The row.
        field: The file field holding the image.
        stored_name: The stored name the re-encode was queued for.
        max_dimension: Longest-edge cap in pixels, or None to keep dimensions.
        convert_webp: Whether to encode as WebP.
        only_if: Further conditions the row must still meet.
        also_set: Further fields to set in the same update as the swap.

    Returns:
        What happened. On ``UNDECODABLE`` nothing was changed; the caller decides what to do with the file.

    Raises:
        OSError: Storage could not read the file or write the re-encoded one; on the S3 backend, any of
            :data:`~urbanlens.dashboard.services.media.held_upload.STORAGE_ERRORS`. That says nothing about the file,
            so it is not ``UNDECODABLE``. A file the swap left unnamed that cannot be deleted is left to
            :func:`sweep_unnamed_files`.
    """
    filters = {"pk": pk, field: stored_name, **(only_if or {})}
    row = rows.filter(**filters).first()
    if row is None:
        return Reencoded.STALE
    stored = getattr(row, field)
    with stored.open("rb") as handle:
        raw = handle.read()
    try:
        data, extension = reencode_image_file(io.BytesIO(raw), max_dimension=max_dimension, convert_webp=convert_webp)
    except (OSError, ValueError, EOFError, SyntaxError, DecompressionBombError) as exc:
        logger.warning("Could not re-encode %s %s's %s: %s", rows.model.__name__, pk, field, exc)
        return Reencoded.UNDECODABLE

    storage = stored.storage
    # Not the uploaded name: it can say as much as the metadata, and icon and avatar paths are served to every member.
    new_name = storage.save(stored.field.generate_filename(row, f"{uuid.uuid4().hex}{extension}"), ContentFile(data))
    replaced = rows.filter(**filters).update(**{field: new_name, **(also_set or {})})
    delete_unnamed_file(storage, stored_name if replaced else new_name)
    return Reencoded.REPLACED if replaced else Reencoded.STALE


def clear_stored_field(rows: QuerySet[Any], pk: int, field: str, stored_name: str, *, also_set: dict[str, Any] | None = None) -> bool:
    """Empty a row's file field and delete the file, if the row still holds that file.

    Args:
        rows: The rows the row is found in.
        pk: The row.
        field: The file field.
        stored_name: The stored name to remove.
        also_set: Further fields to set in the same update.

    Returns:
        Whether the field was cleared.
    """
    holding = rows.filter(pk=pk, **{field: stored_name})
    row = holding.first()
    if row is None or not holding.update(**{field: "", **(also_set or {})}):
        return False
    delete_unnamed_file(getattr(row, field).storage, stored_name)
    return True
