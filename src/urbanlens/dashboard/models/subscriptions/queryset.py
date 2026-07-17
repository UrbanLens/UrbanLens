"""Custom queryset/manager for SubscriptionRole and UserSubscription."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation


class SubscriptionRoleQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for SubscriptionRole models."""

    def get_by_slug(self, slug: str):
        """Return the role with this slug, or None if it doesn't exist.

        Args:
            slug: The role's unique slug.

        Returns:
            The matching SubscriptionRole, or None.
        """
        return self.filter(slug=slug).first()


class SubscriptionRoleManager(abstract.DashboardManager.from_queryset(SubscriptionRoleQuerySet)):
    """Custom query manager for SubscriptionRole models."""


class UserSubscriptionQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for UserSubscription models."""

    def not_revoked(self) -> UserSubscriptionQuerySet:
        """Subscriptions that haven't been explicitly revoked.

        Deliberately does not check ``expires_at`` - unlike ``active()``, this
        also includes grants that have quietly expired but were never
        explicitly revoked (e.g. the site-admin "grants I've issued" list,
        which wants to keep showing an admin's past grants even once they
        lapse).

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

    def granted_by_admin(self, admin_user: User) -> UserSubscriptionQuerySet:
        """Not-revoked subscriptions a given admin has granted.

        Args:
            admin_user: The admin who issued the grants.

        Returns:
            Matching subscriptions.
        """
        return self.not_revoked().filter(granted_by=admin_user)


class UserSubscriptionManager(abstract.DashboardManager.from_queryset(UserSubscriptionQuerySet)):
    """Custom query manager for UserSubscription models."""


class PendingSubscriptionGrantQuerySet(abstract.DashboardQuerySet):
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


class PendingSubscriptionGrantManager(abstract.DashboardManager.from_queryset(PendingSubscriptionGrantQuerySet)):
    """Custom query manager for PendingSubscriptionGrant models."""
