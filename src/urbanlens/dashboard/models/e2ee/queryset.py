"""Querysets for the e2ee package's models.

Pure read-query helpers only - nothing here touches wrapping/sealing logic
or any key material. See ``docs/e2ee.md`` for the scheme itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.group_chats.model import GroupChat
    from urbanlens.dashboard.models.profile.model import Profile


class MessagingKeyBundleQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for MessagingKeyBundle models."""

    def for_profile(self, profile: Profile) -> MessagingKeyBundleQuerySet:
        """The bundle row (at most one - ``profile`` is a OneToOneField) for a profile.

        Args:
            profile: The profile to look up.

        Returns:
            A queryset matching at most one row.
        """
        return self.filter(profile=profile)

    def for_profiles(self, profiles: Iterable[Profile] | Iterable[int]) -> MessagingKeyBundleQuerySet:
        """Bundles for a set of profiles - accepts Profile instances or raw ids.

        Args:
            profiles: Iterable of Profile instances (or profile ids).

        Returns:
            Matching bundles, one per enrolled profile in the set.
        """
        return self.filter(profile__in=profiles)


class MessagingKeyBundleManager(abstract.DashboardManager.from_queryset(MessagingKeyBundleQuerySet)):
    """Custom query manager for MessagingKeyBundle models."""


class ConversationKeyQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for ConversationKey models."""

    def between(self, profile_a: Profile, profile_b: Profile) -> ConversationKeyQuerySet:
        """Every key version for a conversation between two profiles, oldest first.

        Args:
            profile_a: One participant (order doesn't matter).
            profile_b: The other participant.

        Returns:
            Matching rows ordered by ``version`` ascending. Callers that want
            only the latest can chain ``.order_by("-version").first()``.
        """
        from urbanlens.dashboard.models.e2ee.conversation_key import ConversationKey

        low, high = ConversationKey.canonical_pair(profile_a, profile_b)
        return self.filter(profile_low=low, profile_high=high).order_by("version")


class ConversationKeyManager(abstract.DashboardManager.from_queryset(ConversationKeyQuerySet)):
    """Custom query manager for ConversationKey models."""


class GroupKeyQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for GroupKey models."""

    def for_group(self, group: GroupChat) -> GroupKeyQuerySet:
        """Every key version for a group chat.

        Args:
            group: The group chat.

        Returns:
            Matching rows, unordered (callers apply their own ordering).
        """
        return self.filter(group=group)


class GroupKeyManager(abstract.DashboardManager.from_queryset(GroupKeyQuerySet)):
    """Custom query manager for GroupKey models."""
