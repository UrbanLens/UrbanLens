"""Per-conversation-pair consent for receiving images in direct messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.meta import ImagePermissionStatus


class DirectMessageImagePermission(abstract.DashboardModel):
    """A viewer's standing decision on whether to see images sent by a particular sender.

    Created (as PENDING) the first time a sender attaches an image to a
    message for a given viewer. Until ALLOWED, every image from that sender
    renders blurred in the viewer's thread - "Allow Once" reveals a single
    message's images without changing this row (see
    `DirectMessage.images_revealed`).
    """

    viewer = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="+",
    )
    sender = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="+",
    )
    status = CharField(max_length=20, choices=ImagePermissionStatus.choices, default=ImagePermissionStatus.PENDING)

    if TYPE_CHECKING:
        viewer_id: int
        sender_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_dm_image_permissions"
        constraints = [
            UniqueConstraint(fields=["viewer", "sender"], name="db_dm_image_perm_unique"),
        ]
