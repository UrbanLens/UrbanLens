"""Tests for the site-admin subscription role create/rename/delete actions."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.subscriptions import SubscriptionRole, UserSubscription, grant_subscription
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

_SUBSCRIPTIONS_URL = reverse("site_admin_subscriptions")


class RoleCreateActionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin: User = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_creates_a_role_with_a_derived_slug(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_create", "name": "Gold Tier", "description": "Top donors"})
        self.assertEqual(response.status_code, 302)
        role = SubscriptionRole.objects.get(slug="gold-tier")
        self.assertEqual(role.name, "Gold Tier")
        self.assertEqual(role.description, "Top donors")
        self.assertEqual(role.feature_set, set())

    def test_blank_name_is_rejected(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_create", "name": "  "})
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])
        self.assertFalse(SubscriptionRole.objects.exclude(slug="vip").exists())

    def test_duplicate_names_get_distinct_slugs(self) -> None:
        self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_create", "name": "Gold Tier"})
        self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_create", "name": "Gold Tier"})
        slugs = set(SubscriptionRole.objects.exclude(slug="vip").values_list("slug", flat=True))
        self.assertEqual(slugs, {"gold-tier", "gold-tier-2"})

    def test_non_admin_is_forbidden(self) -> None:
        other: User = baker.make(User)
        client = Client()
        client.force_login(other)
        response = client.post(_SUBSCRIPTIONS_URL, {"action": "role_create", "name": "Gold Tier"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SubscriptionRole.objects.filter(slug="gold-tier").exists())


class RoleRenameActionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin: User = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)
        self.role = SubscriptionRole.objects.get(slug="vip")

    def test_renames_the_role(self) -> None:
        response = self.client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "role_rename", "role_slug": "vip", "name": "Premium", "description": "Renamed from VIP"},
        )
        self.assertEqual(response.status_code, 302)
        self.role.refresh_from_db()
        self.assertEqual(self.role.name, "Premium")
        self.assertEqual(self.role.description, "Renamed from VIP")
        self.assertEqual(self.role.slug, "vip", "renaming keeps the slug stable so existing grants keep resolving")

    def test_blank_name_is_rejected(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_rename", "role_slug": "vip", "name": ""})
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])
        self.role.refresh_from_db()
        self.assertEqual(self.role.name, "VIP")

    def test_unknown_role_is_a_no_op(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_rename", "role_slug": "does-not-exist", "name": "Whatever"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])

    def test_non_admin_is_forbidden(self) -> None:
        other: User = baker.make(User)
        client = Client()
        client.force_login(other)
        response = client.post(_SUBSCRIPTIONS_URL, {"action": "role_rename", "role_slug": "vip", "name": "Premium"})
        self.assertEqual(response.status_code, 403)
        self.role.refresh_from_db()
        self.assertEqual(self.role.name, "VIP")


class RoleDeleteActionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin: User = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)
        self.role = SubscriptionRole.objects.get(slug="vip")

    def test_deletes_the_role(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_delete", "role_slug": "vip"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SubscriptionRole.objects.filter(slug="vip").exists())

    def test_deleting_a_role_revokes_grants_via_cascade(self) -> None:
        holder: User = baker.make(User)
        grant_subscription(holder, self.role, self.admin, months=None)
        self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_delete", "role_slug": "vip"})
        self.assertFalse(UserSubscription.objects.filter(user=holder).exists())

    def test_unknown_role_is_a_no_op(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_delete", "role_slug": "does-not-exist"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])
        self.assertTrue(SubscriptionRole.objects.filter(slug="vip").exists())

    def test_non_admin_is_forbidden(self) -> None:
        other: User = baker.make(User)
        client = Client()
        client.force_login(other)
        response = client.post(_SUBSCRIPTIONS_URL, {"action": "role_delete", "role_slug": "vip"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(SubscriptionRole.objects.filter(slug="vip").exists())
