"""QuerySets and Managers for the group chat models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.group_chats.model import GroupChatMembership


class GroupChatQuerySet(abstract.DashboardQuerySet):
    """QuerySet for GroupChat."""


class GroupChatManager(abstract.DashboardManager.from_queryset(GroupChatQuerySet)):
    """Manager for GroupChat."""


class GroupChatMembershipQuerySet(abstract.DashboardQuerySet):
    """QuerySet for GroupChatMembership."""

    def active(self) -> Self:
        """Return only current (not left/removed) memberships.

        Returns:
            Memberships with ``left_at`` unset.
        """
        return self.filter(left_at__isnull=True)


class GroupChatMembershipManager(abstract.DashboardManager.from_queryset(GroupChatMembershipQuerySet)):
    """Manager for GroupChatMembership."""


class GroupMessageQuerySet(abstract.DashboardQuerySet):
    """QuerySet for GroupMessage with membership-scoped visibility helpers."""

    def visible_window(self, membership: GroupChatMembership) -> Self:
        """Restrict to messages the given membership stint is allowed to see.
        A member only sees messages sent during their current stint: nothing from before they joined (the core "added users can't read prior messages" guarantee), and - because leaving ends the stint - nothing from an absence window either.

        Args:
            membership: The viewer's active membership row.

        Returns:
            Messages in the membership's group created at or after the join time.
        """
        return self.filter(group_id=membership.group_id, created__gte=membership.created)

    def mark_read(self, membership: GroupChatMembership) -> None:
        """Advance the membership's read high-water mark to now.

        Args:
            membership: The viewer's active membership row (updated in place).
        """
        now = timezone.now()
        type(membership).objects.filter(pk=membership.pk).update(last_read_at=now)
        membership.last_read_at = now


class GroupMessageManager(abstract.DashboardManager.from_queryset(GroupMessageQuerySet)):
    """Manager for GroupMessage."""
