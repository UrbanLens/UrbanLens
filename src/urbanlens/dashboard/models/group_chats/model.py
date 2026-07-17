"""Group chat models - multi-member conversations built on the direct message system.

A ``GroupChat`` is a named, multi-member conversation. Unlike one-to-one
direct messages (which have no conversation row at all - see
``DirectMessage``), group membership, history visibility, and read state all
hang off explicit rows here:

- ``GroupChatMembership`` - one row per member *stint*. Leaving or being
  removed sets ``left_at`` (the row is kept for history); being re-added
  creates a brand-new row. A member only ever sees messages sent during
  their current stint (``GroupMessageQuerySet.visible_window``), which is
  what guarantees that someone added to a conversation cannot read anything
  sent before they joined.
- ``GroupMessage`` - one message in one group, plaintext or end-to-end
  encrypted (same body-xor-ciphertext contract as ``DirectMessage``; group
  keys live in ``models.e2ee.group_key``).
- ``GroupMessageShare`` - the per-recipient effect of sharing a pin into the
  group: every member gets their own ``PinShare``, exactly as if the sender
  had shared with each of them individually.

Permission model: any active member may rename the group or leave; only the
creator may add or remove members.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, CheckConstraint, DateTimeField, ForeignKey, Index, PositiveIntegerField, Q, TextField, UniqueConstraint, UUIDField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.group_chats.queryset import GroupChatManager, GroupChatMembershipManager, GroupMessageManager
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Maximum length of a group chat's display name.
MAX_GROUP_NAME_LENGTH = 100


class GroupChat(abstract.DashboardModel):
    """A named, multi-member conversation.

    ``uuid`` is the URL key (group ids are not guessable/enumerable).
    ``creator`` holds group-management rights (add/remove members); it is
    nullable only because profile deletion must not delete the group out from
    under the remaining members - a creator-less group simply has no one who
    can manage membership anymore.
    """

    uuid = UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = CharField(max_length=MAX_GROUP_NAME_LENGTH)

    creator = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        related_name="created_group_chats",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        creator_id: int | None

    objects = GroupChatManager()

    def active_memberships(self):
        """Return the current (not left/removed) memberships of this group.

        Returns:
            QuerySet of ``GroupChatMembership`` rows with ``left_at`` unset.
        """
        return self.memberships.filter(left_at__isnull=True)

    def membership_for(self, profile: Profile) -> GroupChatMembership | None:
        """Return `profile`'s active membership in this group, if any.

        Args:
            profile: The profile to look up.

        Returns:
            The active membership row, or None when the profile is not a
            current member.
        """
        return self.active_memberships().filter(profile=profile).first()

    def is_manager(self, profile: Profile) -> bool:
        """Return True when `profile` may manage this group's membership.

        Args:
            profile: The acting profile.

        Returns:
            True when `profile` is the group's creator and still a member.
        """
        return self.creator_id == profile.pk and self.membership_for(profile) is not None

    def __str__(self) -> str:
        """Return a human-readable description of this group.

        Returns:
            String like "GroupChat(3, 'Weekend crew')".
        """
        return f"GroupChat({self.pk}, {self.name!r})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_group_chats"


class GroupChatMembership(abstract.DashboardModel):
    """One member's stint in one group chat.

    ``created`` doubles as the join timestamp: history visibility starts
    there (see ``GroupMessageQuerySet.visible_window``). A member who leaves
    (or is removed) gets ``left_at`` set and loses access entirely; re-adding
    them creates a new row, so the absence window stays invisible to them.
    """

    group = ForeignKey(
        "dashboard.GroupChat",
        on_delete=CASCADE,
        related_name="memberships",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="group_chat_memberships",
    )

    # Set when the member leaves or is removed; null means active.
    left_at = DateTimeField(null=True, blank=True)
    # Who removed them (null for a voluntary leave, or while active).
    removed_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )

    # High-water mark for this member's read state; messages created after
    # this (by other members) count as unread. Null = never opened.
    last_read_at = DateTimeField(null=True, blank=True)

    # Per-member notification muting for this group (mirrors DirectMessageMute).
    muted = BooleanField(default=False)

    if TYPE_CHECKING:
        group_id: int
        profile_id: int
        removed_by_id: int | None

    objects = GroupChatMembershipManager()

    @property
    def is_active(self) -> bool:
        """True while this membership stint is current (not left/removed)."""
        return self.left_at is None

    def end(self, *, removed_by: Profile | None = None) -> None:
        """End this stint now (leave or removal).

        Args:
            removed_by: The manager who removed this member, or None for a
                voluntary leave.
        """
        self.left_at = timezone.now()
        self.removed_by = removed_by
        self.save(update_fields=["left_at", "removed_by", "updated"])

    def __str__(self) -> str:
        """Return a human-readable description of this membership.

        Returns:
            String like "GroupChatMembership(group=3, profile=7, active)".
        """
        status = "active" if self.is_active else "ended"
        return f"GroupChatMembership(group={self.group_id}, profile={self.profile_id}, {status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_group_chat_memberships"
        constraints = [
            # One *active* stint per member per group; ended stints accumulate.
            UniqueConstraint(
                fields=["group", "profile"],
                condition=Q(left_at__isnull=True),
                name="db_group_chat_one_active_membership",
            ),
        ]
        indexes = [
            Index(fields=["profile", "left_at"], name="idxdb_gcm_profile_active"),
        ]


class GroupMessage(abstract.DashboardModel):
    """One message in one group chat.

    Same plaintext-or-encrypted contract as ``DirectMessage``: exactly one of
    ``body``/``ciphertext`` is non-empty, and ``key_version`` records which
    ``GroupKey`` version encrypted it (0 = plaintext). There is no per-member
    delete; ``deleted_at`` is the sender's delete-for-everyone tombstone.
    """

    group = ForeignKey(
        "dashboard.GroupChat",
        on_delete=CASCADE,
        related_name="messages",
    )
    sender = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="sent_group_messages",
    )

    body = TextField(max_length=MAX_DIRECT_MESSAGE_LENGTH, blank=True, default="")

    # End-to-end encrypted alternative to ``body`` - base64 crypto_secretbox
    # output produced in the sender's browser under the group key.
    ciphertext = TextField(blank=True, default="")
    nonce = CharField(max_length=64, blank=True, default="")
    # Which GroupKey.version encrypted this message (0 = plaintext).
    key_version = PositiveIntegerField(default=0)

    # Sender-initiated delete-for-everyone: tombstoned for all members.
    deleted_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        group_id: int
        sender_id: int

    objects = GroupMessageManager()

    @property
    def is_encrypted(self) -> bool:
        """True when this message's content is end-to-end encrypted.

        Returns:
            True when ``ciphertext`` is set (the server cannot read the body).
        """
        return bool(self.ciphertext)

    def share_for(self, viewer_id: int):
        """Return the viewer's own share row on this message, if any.

        Iterates the (typically prefetched) ``shares`` relation rather than
        issuing a fresh query, so rendering a page of messages stays N+1-free.

        Args:
            viewer_id: Primary key of the viewing profile.

        Returns:
            The viewer's ``GroupMessageShare``, or None.
        """
        for share in self.shares.all():
            if share.recipient_id == viewer_id:
                return share
        return None

    def tombstone_text_for(self, viewer_id: int) -> str | None:
        """Return placeholder text to show `viewer_id` instead of this message.

        Mirrors ``DirectMessage.tombstone_text_for``: the sender always sees
        their own message in full.

        Args:
            viewer_id: Primary key of the profile viewing this message.

        Returns:
            Tombstone text, or None when the message renders normally.
        """
        if viewer_id == self.sender_id:
            return None
        if self.deleted_at is not None:
            return "Message deleted"
        return None

    def __str__(self) -> str:
        """Return a human-readable description of this message.

        Returns:
            String like "GroupMessage 3 in group 7: <body prefix>".
        """
        if self.is_encrypted:
            return f"GroupMessage {self.pk} in group {self.group_id}: [encrypted]"
        return f"GroupMessage {self.pk} in group {self.group_id}: {self.body[:60]}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_group_messages"
        ordering = ["created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["group", "created"], name="idxdb_gmsg_group_created"),
        ]
        constraints = [
            # A message is plaintext (body) or encrypted (ciphertext), never both.
            CheckConstraint(
                condition=Q(body="") | Q(ciphertext=""),
                name="db_gmsg_body_xor_ciphertext",
            ),
        ]


class GroupMessageShare(abstract.DashboardModel):
    """One member's copy of a pin shared into a group chat.

    Sharing a pin into a group counts as sharing it with every member
    individually: the service creates one ``PinShare`` per member (running the
    full provenance/exposure pipeline each time) and records each here so the
    thread can render that member's own accept/reject state on the card.
    Members the sender isn't connected to get no row (the friends-only
    sharing rule still applies per member); they see the card without an
    action button.
    """

    message = ForeignKey(
        "dashboard.GroupMessage",
        on_delete=CASCADE,
        related_name="shares",
    )
    recipient = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="group_message_shares",
    )
    pin_share = ForeignKey(
        "dashboard.PinShare",
        on_delete=SET_NULL,
        related_name="group_message_shares",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        message_id: int
        recipient_id: int
        pin_share_id: int | None

    def __str__(self) -> str:
        """Return a human-readable description of this share copy.

        Returns:
            String like "GroupMessageShare(message=3, recipient=7)".
        """
        return f"GroupMessageShare(message={self.message_id}, recipient={self.recipient_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_group_message_shares"
        constraints = [
            UniqueConstraint(fields=["message", "recipient"], name="db_gmsg_share_one_per_recipient"),
        ]
