"""Tests for SubscriptionRole model-level behavior not covered by the queryset tests."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, active_subscription_roles, user_has_feature


class VipRoleSeedTests(TestCase):
    """The built-in "vip" role is seeded once by migration 0019, not recreated at runtime."""

    def test_vip_role_exists_by_default(self) -> None:
        self.assertTrue(SubscriptionRole.objects.filter(slug="vip").exists())

    def test_deleting_it_is_permanent(self) -> None:
        SubscriptionRole.objects.get(slug="vip").delete()
        self.assertFalse(SubscriptionRole.objects.filter(slug="vip").exists())


class UniqueSlugTests(TestCase):
    """unique_slug() derives a slug from admin-entered role names, avoiding collisions."""

    def test_slugifies_the_name(self) -> None:
        self.assertEqual(SubscriptionRole.unique_slug("Gold Tier"), "gold-tier")

    def test_appends_a_suffix_on_collision(self) -> None:
        baker.make(SubscriptionRole, slug="gold-tier")
        self.assertEqual(SubscriptionRole.unique_slug("Gold Tier"), "gold-tier-2")

    def test_walks_past_multiple_collisions(self) -> None:
        baker.make(SubscriptionRole, slug="gold-tier")
        baker.make(SubscriptionRole, slug="gold-tier-2")
        self.assertEqual(SubscriptionRole.unique_slug("Gold Tier"), "gold-tier-3")

    def test_falls_back_to_role_for_unslugifiable_names(self) -> None:
        self.assertEqual(SubscriptionRole.unique_slug("!!!"), "role")


class PaidSubscriptionFeatureGrantTests(TestCase):
    """user_has_feature()/active_subscription_roles() also honor paid RoleSubscriptions."""

    def setUp(self) -> None:
        super().setUp()
        # A prior user absorbs the first-user site-admin bootstrap promotion
        # (see promote_first_user_if_needed) - a site admin bypasses every
        # SiteFeature gate, which would otherwise make self.user appear to
        # have the feature regardless of its RoleSubscription state.
        baker.make(User)
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)

    def test_active_threshold_met_subscription_grants_the_feature(self) -> None:
        baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE, threshold_met=True)
        self.assertTrue(user_has_feature(self.user, SiteFeature.NEARBY_RESEARCH))

    def test_active_threshold_met_subscription_is_in_active_subscription_roles(self) -> None:
        baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE, threshold_met=True)
        self.assertIn(self.role, active_subscription_roles(self.user))

    def test_threshold_not_met_does_not_grant_the_feature(self) -> None:
        baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE, threshold_met=False)
        self.assertFalse(user_has_feature(self.user, SiteFeature.NEARBY_RESEARCH))
        self.assertNotIn(self.role, active_subscription_roles(self.user))

    def test_canceled_subscription_does_not_grant_the_feature(self) -> None:
        baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.CANCELED, threshold_met=True)
        self.assertFalse(user_has_feature(self.user, SiteFeature.NEARBY_RESEARCH))
        self.assertNotIn(self.role, active_subscription_roles(self.user))

    def test_no_subscription_at_all_does_not_grant_the_feature(self) -> None:
        self.assertFalse(user_has_feature(self.user, SiteFeature.NEARBY_RESEARCH))
        self.assertNotIn(self.role, active_subscription_roles(self.user))
