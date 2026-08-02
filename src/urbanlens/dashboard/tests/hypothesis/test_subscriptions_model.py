"""Tests for SubscriptionRole model-level behavior not covered by the queryset tests."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.subscriptions import SubscriptionRole


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
