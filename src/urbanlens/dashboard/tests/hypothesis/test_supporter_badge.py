"""Tests for Profile.is_supporter / Profile.display_supporter_badge.

DB-backed - both properties resolve the user's real subscription state via
active_subscription_roles(), which queries RoleSubscription/UserSubscription.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SubscriptionRole

_hyp_db = settings(max_examples=20, deadline=None)


class IsSupporterTests(TestCase):
    """Profile.is_supporter mirrors active_subscription_roles(user) - paid or admin-granted."""

    def setUp(self) -> None:
        super().setUp()
        # Absorb the first-user site-admin bootstrap promotion so self.user
        # isn't itself treated as a site admin (see test_subscriptions_model.py).
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.role = baker.make(SubscriptionRole)

    def test_no_subscription_is_not_a_supporter(self) -> None:
        self.assertFalse(self.profile.is_supporter)

    def test_active_threshold_met_subscription_is_a_supporter(self) -> None:
        baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.ACTIVE,
            threshold_met=True,
        )
        self.assertTrue(self.profile.is_supporter)

    def test_threshold_not_met_is_not_a_supporter(self) -> None:
        baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.ACTIVE,
            threshold_met=False,
        )
        self.assertFalse(self.profile.is_supporter)

    def test_canceled_subscription_is_not_a_supporter(self) -> None:
        baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.CANCELED,
            threshold_met=True,
        )
        self.assertFalse(self.profile.is_supporter)


class DisplaySupporterBadgeTests(TestCase):
    """display_supporter_badge requires both an active subscription and the opt-in toggle."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.role = baker.make(SubscriptionRole)

    def _make_supporter(self) -> None:
        baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.ACTIVE,
            threshold_met=True,
        )

    def test_supporter_with_badge_enabled_shows_the_badge(self) -> None:
        self._make_supporter()
        self.profile.show_supporter_badge = True
        self.assertTrue(self.profile.display_supporter_badge)

    def test_supporter_with_badge_disabled_hides_the_badge(self) -> None:
        self._make_supporter()
        self.profile.show_supporter_badge = False
        self.assertFalse(self.profile.display_supporter_badge)

    def test_non_supporter_with_badge_enabled_still_hides_the_badge(self) -> None:
        self.profile.show_supporter_badge = True
        self.assertFalse(self.profile.display_supporter_badge)

    @given(show_badge=st.booleans())
    @_hyp_db
    def test_matches_boolean_and_of_toggle_and_subscription(self, show_badge: bool) -> None:
        self.profile.show_supporter_badge = show_badge
        self.assertEqual(self.profile.display_supporter_badge, show_badge and self.profile.is_supporter)
