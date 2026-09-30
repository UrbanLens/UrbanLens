"""QuerySets and Managers for the group chat models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.friendship.blocks import SharedSpaceBlocks
    from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership, GroupMessage  # noqa: F401 - mypy needs these; ruff does not


class GroupChatQuerySet(abstract.DashboardQuerySet["GroupChat"]):
    """QuerySet for GroupChat."""


_GroupChatManagerBase = abstract.DashboardManager.from_queryset(GroupChatQuerySet)


class GroupChatManager(_GroupChatManagerBase):
    """Manager for GroupChat."""


class GroupChatMembershipQuerySet(abstract.DashboardQuerySet["GroupChatMembership"]):
    """QuerySet for GroupChatMembership."""

    def active(self) -> Self:
        """Return only current (not left/removed) memberships.

        Returns:
            Memberships with ``left_at`` unset.
        """
        return self.filter(left_at__isnull=True)


_GroupChatMembershipManagerBase = abstract.DashboardManager.from_queryset(GroupChatMembershipQuerySet)


class GroupChatMembershipManager(_GroupChatMembershipManagerBase):
    """Manager for GroupChatMembership."""


class GroupMessageQuerySet(abstract.DashboardQuerySet["GroupMessage"]):
    """QuerySet for GroupMessage with membership-scoped visibility helpers."""

    def visible_window(self, membership: GroupChatMembership, *, blocks: SharedSpaceBlocks | None = None) -> Self:
        """Restrict to messages the given membership stint is allowed to see.
        A member only sees messages sent during their current stint: nothing from before they joined (the core "added users can't read prior messages" guarantee), and - because leaving ends the stint - nothing from an absence window either.
        Nor anything a member they are in a block with sent once the block was in place (``models.friendship.blocks``).

        Args:
            membership: The viewer's active membership row.
            blocks: The viewer's blocks, when the caller already resolved them.

        Returns:
            Messages in the membership's group created at or after the join time, less the block-hidden ones.
        """
        from urbanlens.dashboard.models.friendship.blocks import SharedSpaceBlocks

        blocks = blocks if blocks is not None else SharedSpaceBlocks.for_viewer(membership.profile_id)
        return blocks.exclude_hidden(self.filter(group_id=membership.group_id, created__gte=membership.created), author_field="sender_id")

    def mark_read(self, membership: GroupChatMembership) -> None:
        """Advance the membership's read high-water mark to now.

        Args:
            membership: The viewer's active membership row (updated in place).
        """
        now = timezone.now()
        type(membership).objects.filter(pk=membership.pk).update(last_read_at=now)
        membership.last_read_at = now


_GroupMessageManagerBase = abstract.DashboardManager.from_queryset(GroupMessageQuerySet)


class GroupMessageManager(_GroupMessageManagerBase):
    """Manager for GroupMessage."""
