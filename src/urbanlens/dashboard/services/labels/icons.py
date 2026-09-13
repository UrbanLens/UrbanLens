"""A label's custom icon is shrunk by the sandbox worker, not by the request that uploads it.

The upload is size-checked, content-sniffed and malware-scanned in the request and stored as it
came. Decoding it to resize is an ``untrusted_parse`` operation, so that happens here, afterwards;
until it has, the label shows the upload itself, decoded by the browser rather than the server.
"""

from __future__ import annotations

import io
import logging
import os
from typing import IO, TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image as PILImage
from PIL.Image import DecompressionBombError

from urbanlens.dashboard.services.sandbox.guard import untrusted_parse

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label

logger = logging.getLogger(__name__)

#: The longest side a stored label icon keeps.
ICON_MAX_PX = 256


def queue_icon_resize(label: Label) -> None:
    """Ask the sandbox worker to shrink *label*'s icon once this transaction commits.

    Args:
        label: A saved label whose ``custom_icon`` was just set.
    """
    if not label.custom_icon:
        return
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import resize_label_icon

    label_id, icon_name = label.pk, label.custom_icon.name
    transaction.on_commit(lambda: safely_enqueue_task(resize_label_icon, label_id, icon_name))


@untrusted_parse("image.icon")
def shrink_icon(stored: IO[bytes], name: str) -> tuple[bytes, str] | None:
    """Re-encode an icon to at most :data:`ICON_MAX_PX` on its longest side.

    Args:
        stored: The icon's bytes.
        name: Its current file name, whose stem the new name keeps.

    Returns:
        ``(bytes, file name)`` for the shrunk icon, or None when it is already small enough
        or cannot be decoded as an image.
    """
    try:
        img: PILImage.Image = PILImage.open(stored)
        if max(img.width, img.height) <= ICON_MAX_PX:
            return None
        img = img.convert("RGBA") if img.mode in {"RGBA", "P", "PA"} else img.convert("RGB")
        img.thumbnail((ICON_MAX_PX, ICON_MAX_PX), PILImage.Resampling.LANCZOS)
        fmt = "PNG" if img.mode == "RGBA" else "JPEG"
        out = io.BytesIO()
        img.save(out, format=fmt, quality=88, optimize=True)
    except (OSError, ValueError, DecompressionBombError):
        logger.info("Label icon %s could not be decoded for resizing", name)
        return None
    stem = os.path.basename(name).rsplit(".", 1)[0] or "icon"
    return out.getvalue(), f"{stem}.{'png' if fmt == 'PNG' else 'jpg'}"


def resize_stored_icon(label_id: int, icon_name: str) -> bool:
    """Shrink a label's stored icon, if the label still has the icon the resize was queued for.

    The swap is a conditional update on the stored name, so a label edited or deleted meanwhile
    keeps what it has, and whichever file lost is deleted. The label's pins are touched so maps
    redraw the new icon.

    Args:
        label_id: The label.
        icon_name: The stored name its icon had when the resize was queued.

    Returns:
        Whether the icon was replaced.
    """
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_labels

    label = Label.objects.filter(pk=label_id, custom_icon=icon_name).first()
    if label is None:
        return False
    with label.custom_icon.open("rb") as stored:
        shrunk = shrink_icon(stored, icon_name)
    if shrunk is None:
        return False
    data, filename = shrunk
    storage = label.custom_icon.storage
    new_name = storage.save(label.custom_icon.field.generate_filename(label, filename), ContentFile(data))
    replaced = Label.objects.filter(pk=label_id, custom_icon=icon_name).update(custom_icon=new_name, updated=timezone.now())
    storage.delete(icon_name if replaced else new_name)
    if replaced:
        touch_pins_for_labels([label_id])
    return bool(replaced)
