"""A label's custom icon is re-encoded by the sandbox worker, not by the request that uploads it.

The upload is size-checked, content-sniffed and malware-scanned in the request and stored as it
came. Decoding it is an ``untrusted_parse`` operation, so that happens here, afterwards; until it
has, the label shows the upload itself, decoded by the browser rather than the server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label

#: The longest side a stored label icon keeps.
ICON_MAX_PX = 256


def queue_icon_resize(label: Label) -> None:
    """Ask the sandbox worker to re-encode *label*'s icon once this transaction commits.

    Args:
        label: A saved label whose ``custom_icon`` was just set.
    """
    if not label.custom_icon:
        return
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import resize_label_icon

    label_id, icon_name = label.pk, label.custom_icon.name
    transaction.on_commit(lambda: safely_enqueue_task(resize_label_icon, label_id, icon_name))


def resize_stored_icon(label_id: int, icon_name: str) -> bool:
    """Re-encode a label's stored icon as WebP of at most :data:`ICON_MAX_PX`, if the label still has that icon.

    Every icon is rewritten, whatever its size, so none is served with the metadata it was uploaded with. One that
    cannot be decoded is removed. A label deleted meanwhile keeps its file, which undo restores. The label's pins are
    touched so maps redraw the new icon.

    Args:
        label_id: The label.
        icon_name: The stored name its icon had when the re-encode was queued.

    Returns:
        Whether the icon was replaced or removed.
    """
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_labels
    from urbanlens.dashboard.services.media.stored_field import Reencoded, clear_stored_field, reencode_stored_field

    outcome = reencode_stored_field(Label.objects.all(), label_id, "custom_icon", icon_name, max_dimension=ICON_MAX_PX, convert_webp=True, also_set={"updated": timezone.now()})
    changed = outcome is Reencoded.REPLACED or (outcome is Reencoded.UNDECODABLE and clear_stored_field(Label.objects.all(), label_id, "custom_icon", icon_name, also_set={"updated": timezone.now()}))
    if changed:
        touch_pins_for_labels([label_id])
    return changed
