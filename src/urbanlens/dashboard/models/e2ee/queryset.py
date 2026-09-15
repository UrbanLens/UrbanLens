"""Querysets for the e2ee package's models.

Pure read-query helpers only - nothing here touches wrapping/sealing logic
or any key material. See ``docs/designs/e2ee.md`` for the scheme itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Exists, OuterRef, Q

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


class E2EEPasskeyWrapQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for E2EEPasskeyWrap models."""

    def usable_for_bundle(self, bundle) -> E2EEPasskeyWrapQuerySet:
        """Wraps that can still unwrap this bundle's current keypair.
        A wrap whose ``bundle_version`` lags the bundle encrypts a superseded private key (a reset happened without the cleanup running) - serving it would produce an unlock that silently yields the wrong identity.

        Args:
            bundle: The MessagingKeyBundle being unlocked.

        Returns:
            Wraps for this bundle at its current version.
        """
        return self.filter(bundle=bundle, bundle_version=bundle.version)


class E2EEPasskeyWrapManager(abstract.DashboardManager.from_queryset(E2EEPasskeyWrapQuerySet)):
    """Custom query manager for E2EEPasskeyWrap models."""


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

    def with_outside_holders(self) -> GroupKeyQuerySet:
        """Annotate ``has_outside_holder``: whether anyone outside the group's active membership holds this version.

        An envelope whose profile was deleted counts as outside, since that person may still hold the key.

        Returns:
            The queryset, annotated.
        """
        from urbanlens.dashboard.models.e2ee.group_key import GroupKeyEnvelope
        from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

        active_members = GroupChatMembership.objects.active().filter(group_id=OuterRef(OuterRef("group_id"))).values("profile_id")
        outside = GroupKeyEnvelope.objects.filter(key_id=OuterRef("pk")).filter(Q(profile__isnull=True) | ~Q(profile_id__in=active_members))
        return self.annotate(has_outside_holder=Exists(outside))


class GroupKeyManager(abstract.DashboardManager.from_queryset(GroupKeyQuerySet)):
    """Custom query manager for GroupKey models."""
