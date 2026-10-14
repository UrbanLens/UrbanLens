"""Tests for SubscriptionRole model-level behavior not covered by the queryset tests."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import (
    SubscriptionRole,
)
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group


class VipRoleSeedTests(TestCase):
    """
    The built-in "vip" role is seeded once by the initial migration, not recreated at runtime.
    """

    def setUp(self) -> None:
        super().setUp()
        SubscriptionRole.objects.filter(slug="vip").delete()

    def test_deleting_it_is_permanent(self) -> None:
        baker.make(SubscriptionRole, slug="vip", name="VIP")
        SubscriptionRole.objects.get(slug="vip").delete()
        admin = baker.make(User)
        add_user_to_site_admin_group(admin)
        Profile.objects.filter(user=admin).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(admin)

        self.assertEqual(self.client.get(reverse("site_admin_subscriptions")).status_code, 200)
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
