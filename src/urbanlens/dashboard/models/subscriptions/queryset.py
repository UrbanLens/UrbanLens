"""Custom queryset/manager for SubscriptionRole and UserSubscription."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.subscriptions.access_state import AccessBearingQuerySet

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
    from urbanlens.dashboard.models.subscriptions.model import (  # noqa: F401 - mypy needs these; ruff does not
        PendingSubscriptionGrant,
        SubscriptionRole,
        UserSubscription,
    )


class SubscriptionRoleQuerySet(AccessBearingQuerySet, abstract.DashboardQuerySet["SubscriptionRole"]):
    """Custom queryset for SubscriptionRole models."""

    def get_by_slug(self, slug: str):
        """Return the role with this slug, or None if it doesn't exist.

        Args:
            slug: The role's unique slug.

        Returns:
            The matching SubscriptionRole, or None.
        """
        return self.filter(slug=slug).first()


_SubscriptionRoleManagerBase = abstract.DashboardManager.from_queryset(SubscriptionRoleQuerySet)


class SubscriptionRoleManager(_SubscriptionRoleManagerBase):
    """Custom query manager for SubscriptionRole models."""


class UserSubscriptionQuerySet(AccessBearingQuerySet, abstract.DashboardQuerySet["UserSubscription"]):
    """Custom queryset for UserSubscription models."""

    def not_revoked(self) -> UserSubscriptionQuerySet:
        """Subscriptions that haven't been explicitly revoked.

        Returns:
            Matching subscriptions.
        """
        return self.filter(revoked_at__isnull=True)

    def active(self) -> UserSubscriptionQuerySet:
        """Subscriptions that are both not revoked and not expired.

        Returns:
            Matching subscriptions.
        """
        now = timezone.now()
        return self.not_revoked().filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

    def active_for(self, user: User) -> UserSubscriptionQuerySet:
        """A user's currently-active (not revoked, not expired) subscriptions.

        Args:
            user: The user to look up.

        Returns:
            Matching subscriptions.
        """
        return self.active().filter(user=user)

    def grants_for_site_admin(self) -> UserSubscriptionQuerySet:
        """Every not-revoked grant, whoever made it, with what the grant list shows of each preloaded.

        Returns:
            Matching subscriptions.
        """
        return self.not_revoked().select_related("user", "role", "granted_by")


_UserSubscriptionManagerBase = abstract.DashboardManager.from_queryset(UserSubscriptionQuerySet)


class UserSubscriptionManager(_UserSubscriptionManagerBase):
    """Custom query manager for UserSubscription models."""


class PendingSubscriptionGrantQuerySet(abstract.DashboardQuerySet["PendingSubscriptionGrant"]):
    """Custom queryset for PendingSubscriptionGrant models."""

    def for_invitation(self, invitation: FriendInvitation) -> PendingSubscriptionGrantQuerySet:
        """Grants attached to one invitation, ready to redeem once it's accepted.

        Args:
            invitation: The invitation whose pending grants to return.

        Returns:
            Matching grants, with ``role``/``granted_by`` preloaded since every
            caller immediately reads both while applying the grant.
        """
        return self.filter(invitation=invitation).select_related("role", "granted_by")


_PendingSubscriptionGrantManagerBase = abstract.DashboardManager.from_queryset(PendingSubscriptionGrantQuerySet)


class PendingSubscriptionGrantManager(_PendingSubscriptionGrantManagerBase):
    """Custom query manager for PendingSubscriptionGrant models."""
