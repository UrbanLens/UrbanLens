"""Per-conversation notification muting for direct messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.queryset import DirectMessageMuteManager


class DirectMessageMute(abstract.DashboardModel):
    """A viewer's standing choice to stop being notified about a sender's messages.

    Muting only suppresses notifications (in-app NotificationLog rows and the
    delayed "new message" email) - the conversation itself, unread counts, and
    message delivery are all unaffected. Existence of the row is the mute
    state; there is no separate boolean.
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

    objects = DirectMessageMuteManager()

    if TYPE_CHECKING:
        viewer_id: int
        sender_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_dm_mutes"
        constraints = [
            UniqueConstraint(fields=["viewer", "sender"], name="db_dm_mute_unique"),
        ]
