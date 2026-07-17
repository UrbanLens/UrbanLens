"""QuerySets and Managers for the group chat models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.group_chats.model import GroupChatMembership
    from urbanlens.dashboard.models.profile.model import Profile


class GroupChatQuerySet(abstract.DashboardQuerySet):
    """QuerySet for GroupChat."""

    def for_member(self, profile: Profile) -> Self:
        """Return groups where `profile` is currently an active member.

        Args:
            profile: The member profile.

        Returns:
            Groups with an active (not left/removed) membership for the profile.
        """
        return self.filter(memberships__profile=profile, memberships__left_at__isnull=True).distinct()


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

        A member only sees messages sent during their current stint: nothing
        from before they joined (the core "added users can't read prior
        messages" guarantee), and - because leaving ends the stint - nothing
        from an absence window either.

        Args:
            membership: The viewer's active membership row.

        Returns:
            Messages in the membership's group created at or after the join time.
        """
        return self.filter(group_id=membership.group_id, created__gte=membership.created)

    def unread_for(self, membership: GroupChatMembership) -> Self:
        """Return the messages this member hasn't read yet.

        Args:
            membership: The viewer's active membership row.

        Returns:
            Visible messages from *other* members created after the
            membership's ``last_read_at`` mark (or all of them when the
            thread has never been opened).
        """
        queryset = self.visible_window(membership).exclude(sender_id=membership.profile_id)
        if membership.last_read_at is not None:
            queryset = queryset.filter(created__gt=membership.last_read_at)
        return queryset

    def mark_read(self, membership: GroupChatMembership) -> None:
        """Advance the membership's read high-water mark to now.

        Args:
            membership: The viewer's active membership row (updated in place).
        """
        now = timezone.now()
        type(membership).objects.filter(pk=membership.pk).update(last_read_at=now)
        membership.last_read_at = now

    def search_visible_to(self, profile: Profile) -> Self:
        """Return every plaintext message `profile` may see across all their groups.

        Args:
            profile: The viewing profile.

        Returns:
            Messages within the visibility window of any of the profile's
            active memberships, excluding encrypted bodies (the server cannot
            search what it cannot read).
        """
        from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

        memberships = GroupChatMembership.objects.active().filter(profile=profile)
        visibility = Q(pk__in=[])
        for membership in memberships:
            visibility |= Q(group_id=membership.group_id, created__gte=membership.created)
        return self.filter(visibility).exclude(body="")


class GroupMessageManager(abstract.DashboardManager.from_queryset(GroupMessageQuerySet)):
    """Manager for GroupMessage."""
