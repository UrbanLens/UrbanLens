"""Re-encode an image held in a model's file field: a comment image, an icon or an avatar.

The swap is a conditional update on the stored name, so a row edited or deleted meanwhile keeps what it has, and
whichever file lost is deleted.
"""

from __future__ import annotations

import enum
import io
import logging
from typing import TYPE_CHECKING, Any
import uuid

from django.core.files.base import ContentFile
from PIL.Image import DecompressionBombError

from urbanlens.dashboard.services.media.held_upload import STORAGE_ERRORS
from urbanlens.dashboard.services.media.images import reencode_image_file

if TYPE_CHECKING:
    from django.core.files.storage import Storage
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)


def delete_unnamed_file(storage: Storage, model: str, field: str, name: str) -> None:
    """Delete a file its row no longer names, queueing the delete for later when storage refuses it now.

    A refused delete does not undo or fail what made the row stop naming the file. The file is not left behind either,
    since the media gate serves any icon or avatar path to every member; one the broker would not queue is logged as an
    error naming it.

    Args:
        storage: The field's storage.
        model: The model's label, e.g. ``"dashboard.Label"``.
        field: The file field that named it.
        name: The stored name.
    """
    try:
        storage.delete(name)
    except STORAGE_ERRORS:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import delete_lost_stored_file

        if safely_enqueue_task(delete_lost_stored_file, model, field, name) is None:
            logger.exception("Could not delete %s, which no %s row names, nor queue deleting it later; it stays until deleted by hand", name, model)
        else:
            logger.warning("Could not delete %s, which its row no longer names; queued to try again", name, exc_info=True)


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
            so it is not ``UNDECODABLE``. A file the swap left unnamed that cannot be deleted is deleted later, by
            :func:`delete_unnamed_file`.
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
    delete_unnamed_file(storage, rows.model._meta.label, field, stored_name if replaced else new_name)  # noqa: SLF001 - _meta is Django's public model API
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
    delete_unnamed_file(getattr(row, field).storage, rows.model._meta.label, field, stored_name)  # noqa: SLF001 - _meta is Django's public model API
    return True
