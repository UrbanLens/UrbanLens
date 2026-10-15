"""
Tests for Profile.holds_role / Profile.display_member_badge.

DB-backed - both properties resolve the user's real role state via active_subscription_roles().
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SubscriptionRole, UserSubscription

_hyp_db = settings(max_examples=20, deadline=None)


class HoldsRoleTests(TestCase):
    """Profile.holds_role mirrors active_subscription_roles(user)."""

    def setUp(self) -> None:
        super().setUp()
        # Absorb the first-user site-admin bootstrap promotion so self.user
        # isn't itself treated as a site admin (see test_subscriptions_model.py).
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.role = baker.make(SubscriptionRole)

    def test_no_role_is_not_a_member(self) -> None:
        self.assertFalse(self.profile.holds_role)

    def test_a_granted_role_makes_a_member(self) -> None:
        baker.make(UserSubscription, user=self.user, role=self.role)
        self.assertTrue(self.profile.holds_role)

    def test_a_role_that_has_not_expired_makes_a_member(self) -> None:
        baker.make(UserSubscription, user=self.user, role=self.role, expires_at=timezone.now() + timedelta(days=3))
        self.assertTrue(self.profile.holds_role)

    def test_an_expired_role_is_not_a_member(self) -> None:
        baker.make(UserSubscription, user=self.user, role=self.role, expires_at=timezone.now() - timedelta(days=1))
        self.assertFalse(self.profile.holds_role)

    def test_a_revoked_role_is_not_a_member(self) -> None:
        baker.make(UserSubscription, user=self.user, role=self.role, revoked_at=timezone.now())
        self.assertFalse(self.profile.holds_role)


class DisplayMemberBadgeTests(TestCase):
    """display_member_badge requires both an active role and the opt-in toggle."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.role = baker.make(SubscriptionRole)

    def _make_member(self) -> None:
        baker.make(UserSubscription, user=self.user, role=self.role)

    def test_member_with_badge_enabled_shows_the_badge(self) -> None:
        self._make_member()
        self.profile.show_member_badge = True
        self.assertTrue(self.profile.display_member_badge)

    def test_member_with_badge_disabled_hides_the_badge(self) -> None:
        self._make_member()
        self.profile.show_member_badge = False
        self.assertFalse(self.profile.display_member_badge)

    def test_non_member_with_badge_enabled_still_hides_the_badge(self) -> None:
        self.profile.show_member_badge = True
        self.assertFalse(self.profile.display_member_badge)

    @given(show_badge=st.booleans())
    @_hyp_db
    def test_matches_boolean_and_of_toggle_and_role(self, show_badge: bool) -> None:
        self.profile.show_member_badge = show_badge
        self.assertEqual(self.profile.display_member_badge, show_badge and self.profile.holds_role)
