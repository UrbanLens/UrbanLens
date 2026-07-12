"""DirectMessage model - private one-to-one messages between two profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, CheckConstraint, DateTimeField, F, ForeignKey, Index, PositiveIntegerField, Q, TextField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.meta import RETENTION_DELTAS, MessageRetentionChoice
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

    body = TextField(max_length=MAX_DIRECT_MESSAGE_LENGTH, blank=True, default="")
    read_at = DateTimeField(null=True, blank=True)

    # End-to-end encrypted alternative to ``body`` (exactly one of the two may
    # be non-empty). Base64 crypto_secretbox output produced in the sender's
    # browser under the conversation key - the server cannot read it.
    ciphertext = TextField(blank=True, default="")
    # Base64 random nonce used for ``ciphertext``.
    nonce = CharField(max_length=64, blank=True, default="")
    # Which ConversationKey.version encrypted this message (0 = plaintext).
    key_version = PositiveIntegerField(default=0)

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

    # Quoted message this one is replying to, rendered as a styled quote box.
    reply_to = ForeignKey(
        "self",
        on_delete=SET_NULL,
        related_name="replies",
        null=True,
        blank=True,
    )

    # Sender-initiated delete: tombstoned ("Message deleted") for both parties.
    deleted_by_sender_at = DateTimeField(null=True, blank=True)
    # Recipient-initiated delete: hidden only in the recipient's own view - the
    # sender's copy is completely unaffected and unaware.
    deleted_by_recipient_at = DateTimeField(null=True, blank=True)

    # Snapshot of sender.direct_message_delete_after at send time (see
    # MessageRetentionChoice) - later changes to the sender's setting only
    # affect messages sent afterward.
    sender_delete_after = CharField(
        max_length=20,
        choices=MessageRetentionChoice.choices,
        default=MessageRetentionChoice.NEVER,
    )

    # Set when the recipient chooses "Allow Once" on a blurred image, revealing
    # this message's images without changing the standing per-sender permission.
    images_revealed = BooleanField(default=False)

    # An attached/customized map (plain "attach a map", or a customized pin share).
    markup_map = ForeignKey(
        "dashboard.MarkupMap",
        on_delete=SET_NULL,
        related_name="direct_messages",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        sender_id: int
        recipient_id: int
        reply_to_id: int | None
        markup_map_id: int | None

    objects = DirectMessageManager()

    @property
    def is_encrypted(self) -> bool:
        """True when this message's content is end-to-end encrypted.

        Returns:
            True when ``ciphertext`` is set (the server cannot read the body).
        """
        return bool(self.ciphertext)

    @property
    def is_unread(self) -> bool:
        """True while the recipient has not read this message yet."""
        return self.read_at is None

    @property
    def is_expired_for_recipient(self) -> bool:
        """True once this message's disappearing-message timer has elapsed.

        Only ever hides the message from the recipient - the sender always
        keeps their own copy. Unread messages never expire (the timer starts
        at `read_at`), regardless of how long they've sat unread.

        Returns:
            True when the recipient's view of this message should show a
            tombstone instead of its content.
        """
        if self.sender_delete_after == MessageRetentionChoice.NEVER or self.read_at is None:
            return False
        if self.sender_delete_after == MessageRetentionChoice.WHEN_READ:
            return True
        delta = RETENTION_DELTAS.get(self.sender_delete_after)
        if delta is None:
            return False
        return timezone.now() >= self.read_at + delta

    def tombstone_text_for(self, viewer_id: int) -> str | None:
        """Return placeholder text to show `viewer_id` instead of this message's content.

        The sender always sees their own original message in full, regardless
        of any delete/expiry state - callers must never hide a message from
        its own sender (see the module docstring's consent policy).

        Args:
            viewer_id: Primary key of the profile viewing this message.

        Returns:
            Tombstone text, or None if the message should render normally for
            this viewer.
        """
        if viewer_id == self.sender_id:
            return None
        if self.deleted_by_sender_at is not None:
            return "Message deleted"
        if self.deleted_by_recipient_at is not None:
            return "You removed this message"
        if self.is_expired_for_recipient:
            return "This message is no longer available"
        return None

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
        if self.is_encrypted:
            return f"DM {self.sender_id} -> {self.recipient_id}: [encrypted]"
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
            # A message is plaintext (body) or encrypted (ciphertext), never both.
            CheckConstraint(
                condition=Q(body="") | Q(ciphertext=""),
                name="db_dm_body_xor_ciphertext",
            ),
        ]
