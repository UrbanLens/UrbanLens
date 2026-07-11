"""DirectMessage model - private one-to-one messages between two profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CheckConstraint, DateTimeField, F, ForeignKey, Index, Q, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.queryset import DirectMessageManager
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class DirectMessage(abstract.DashboardModel):
    """One private message from one profile to another.

    A "conversation" is not its own model - it is simply the set of messages
    between two profiles in either direction (see
    ``DirectMessageQuerySet.between`` / ``conversation_rows``). ``read_at``
    doubles as the unread flag: null means the recipient hasn't opened the
    conversation since this message arrived.

    Whether a profile may message another is governed by the recipient's
    ``Profile.direct_message_visibility`` privacy setting, evaluated in
    ``services.direct_messages.can_direct_message`` - never bypass that check
    when creating rows outside of ``create_direct_message``.
    """

    body = TextField(max_length=MAX_DIRECT_MESSAGE_LENGTH)
    read_at = DateTimeField(null=True, blank=True)

    sender = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="sent_direct_messages",
    )
    recipient = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="received_direct_messages",
    )

    if TYPE_CHECKING:
        sender_id: int
        recipient_id: int

    objects = DirectMessageManager()

    @property
    def is_unread(self) -> bool:
        """True while the recipient has not read this message yet."""
        return self.read_at is None

    def partner_for(self, profile: Profile) -> Profile:
        """Return the other participant of this message's conversation.

        Args:
            profile: One of the two participants.

        Returns:
            The sender when ``profile`` is the recipient, else the recipient.
        """
        return self.sender if self.recipient_id == profile.pk else self.recipient

    def __str__(self) -> str:
        """Return a human-readable description of this message.

        Returns:
            String like "DM <sender id> -> <recipient id>: <body prefix>".
        """
        return f"DM {self.sender_id} -> {self.recipient_id}: {self.body[:60]}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_direct_messages"
        ordering = ["created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["sender", "recipient"], name="idxdb_dm_sender_recipient"),
            Index(fields=["recipient", "read_at"], name="idxdb_dm_recipient_read"),
        ]
        constraints = [
            CheckConstraint(
                condition=~Q(sender=F("recipient")),
                name="db_dm_no_self_message",
            ),
        ]
