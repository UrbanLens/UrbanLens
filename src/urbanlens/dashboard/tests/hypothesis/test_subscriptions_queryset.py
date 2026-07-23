"""Tests for SubscriptionRoleQuerySet/UserSubscriptionQuerySet.

Part of the ongoing "every model gets its own queryset/manager" cleanup -
these two models were still on the bare default manager despite several
genuinely duplicated call-site shapes across controllers/subscriptions/model.py
itself (SubscriptionRole.objects.filter(slug=...).first(), and three distinct
UserSubscription "not revoked"/"active" shapes).
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.subscriptions.model import PendingSubscriptionGrant, SubscriptionRole, UserSubscription


class SubscriptionRoleGetBySlugTests(TestCase):
    def test_returns_the_matching_role(self) -> None:
        role = baker.make(SubscriptionRole, slug="explorer")
        self.assertEqual(SubscriptionRole.objects.get_by_slug("explorer"), role)

    def test_returns_none_for_an_unknown_slug(self) -> None:
        self.assertIsNone(SubscriptionRole.objects.get_by_slug("does-not-exist"))


class UserSubscriptionNotRevokedTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.admin = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def test_excludes_revoked_subscriptions(self) -> None:
        active = baker.make(UserSubscription, user=self.user, role=self.role, granted_by=self.admin, revoked_at=None)
        baker.make(UserSubscription, user=self.user, role=self.role, granted_by=self.admin, revoked_at=timezone.now())
        self.assertEqual(list(UserSubscription.objects.not_revoked()), [active])

    def test_includes_an_expired_but_not_revoked_subscription(self) -> None:
        """not_revoked() deliberately ignores expiry - unlike active() below - since the
        site-admin "grants I've issued" list wants to keep showing an admin's past grants
        even once they lapse, not just the currently-usable ones."""
        expired = baker.make(
            UserSubscription,
            user=self.user,
            role=self.role,
            granted_by=self.admin,
            revoked_at=None,
            expires_at=timezone.now() - datetime.timedelta(days=1),
        )
        self.assertEqual(list(UserSubscription.objects.not_revoked()), [expired])


class UserSubscriptionActiveTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.admin = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def test_indefinite_subscription_is_active(self) -> None:
        sub = baker.make(UserSubscription, user=self.user, role=self.role, granted_by=self.admin, revoked_at=None, expires_at=None)
        self.assertEqual(list(UserSubscription.objects.active()), [sub])

    def test_expired_subscription_is_excluded(self) -> None:
        baker.make(
            UserSubscription,
            user=self.user,
            role=self.role,
            granted_by=self.admin,
            revoked_at=None,
            expires_at=timezone.now() - datetime.timedelta(days=1),
        )
        self.assertEqual(list(UserSubscription.objects.active()), [])

    def test_revoked_subscription_is_excluded_even_if_not_yet_expired(self) -> None:
        baker.make(
            UserSubscription,
            user=self.user,
            role=self.role,
            granted_by=self.admin,
            revoked_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(days=30),
        )
        self.assertEqual(list(UserSubscription.objects.active()), [])

    def test_active_for_scopes_to_one_user(self) -> None:
        other_user = baker.make(User)
        mine = baker.make(UserSubscription, user=self.user, role=self.role, granted_by=self.admin, revoked_at=None)
        baker.make(UserSubscription, user=other_user, role=self.role, granted_by=self.admin, revoked_at=None)
        self.assertEqual(list(UserSubscription.objects.active_for(self.user)), [mine])


class UserSubscriptionGrantedByAdminTests(TestCase):
    def test_scopes_to_the_admin_and_excludes_revoked(self) -> None:
        user = baker.make(User)
        admin = baker.make(User)
        other_admin = baker.make(User)
        role_a, role_b, role_c = baker.make(SubscriptionRole, _quantity=3)
        # A revoked grant on the same (user, role) as `mine` is otherwise blocked by
        # unique_active_user_subscription_role, which only allows one non-revoked
        # subscription per (user, role) at a time - use distinct roles per row so
        # each assertion targets exactly the dimension it's testing.
        mine = baker.make(UserSubscription, user=user, role=role_a, granted_by=admin, revoked_at=None)
        baker.make(UserSubscription, user=user, role=role_b, granted_by=admin, revoked_at=timezone.now())
        baker.make(UserSubscription, user=user, role=role_c, granted_by=other_admin, revoked_at=None)
        self.assertEqual(list(UserSubscription.objects.granted_by_admin(admin)), [mine])


class PendingSubscriptionGrantForInvitationTests(TestCase):
    def setUp(self) -> None:
        self.inviter = baker.make(User).profile
        self.admin = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def test_returns_grants_for_the_given_invitation(self) -> None:
        invitation = FriendInvitation.objects.create(inviter=self.inviter, email="invitee@example.com")
        grant = PendingSubscriptionGrant.objects.create(invitation=invitation, role=self.role, granted_by=self.admin)
        self.assertEqual(list(PendingSubscriptionGrant.objects.for_invitation(invitation)), [grant])

    def test_excludes_grants_on_other_invitations(self) -> None:
        invitation = FriendInvitation.objects.create(inviter=self.inviter, email="invitee@example.com")
        other_invitation = FriendInvitation.objects.create(inviter=self.inviter, email="someone-else@example.com")
        PendingSubscriptionGrant.objects.create(invitation=other_invitation, role=self.role, granted_by=self.admin)
        self.assertEqual(list(PendingSubscriptionGrant.objects.for_invitation(invitation)), [])

    def test_empty_for_an_invitation_with_no_grants(self) -> None:
        invitation = FriendInvitation.objects.create(inviter=self.inviter, email="invitee@example.com")
        self.assertEqual(list(PendingSubscriptionGrant.objects.for_invitation(invitation)), [])
