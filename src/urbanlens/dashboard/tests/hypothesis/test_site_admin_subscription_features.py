"""Tests for the site-admin "role features" and "default features" toggle actions."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group

_SUBSCRIPTIONS_URL = reverse("site_admin_subscriptions")


class RoleFeaturesActionTests(TestCase):
    """POST action=role_features toggles which SiteFeature values a role grants."""

    def setUp(self) -> None:
        super().setUp()
        self.admin: User = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_selected_features_are_saved(self) -> None:
        role = baker.make(SubscriptionRole, slug="explorer", features="")
        response = self.client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "role_features", "role_slug": role.slug, "features": [SiteFeature.AI, SiteFeature.SEARCH]},
        )
        self.assertEqual(response.status_code, 302)
        role.refresh_from_db()
        self.assertEqual(role.feature_set, {SiteFeature.AI, SiteFeature.SEARCH})

    def test_unchecking_every_box_clears_features(self) -> None:
        role = baker.make(SubscriptionRole, slug="explorer", features=SiteFeature.AI)
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "role_features", "role_slug": role.slug})
        self.assertEqual(response.status_code, 302)
        role.refresh_from_db()
        self.assertEqual(role.feature_set, set())

    def test_unknown_feature_values_are_ignored(self) -> None:
        role = baker.make(SubscriptionRole, slug="explorer", features="")
        response = self.client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "role_features", "role_slug": role.slug, "features": [SiteFeature.AI, "not_a_real_feature"]},
        )
        self.assertEqual(response.status_code, 302)
        role.refresh_from_db()
        self.assertEqual(role.feature_set, {SiteFeature.AI})

    def test_unknown_role_is_a_no_op(self) -> None:
        response = self.client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "role_features", "role_slug": "does-not-exist", "features": [SiteFeature.AI]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])

    def test_htmx_request_returns_oob_chip_fragment(self) -> None:
        role = baker.make(SubscriptionRole, slug="explorer", features="")
        response = self.client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "role_features", "role_slug": role.slug, "features": [SiteFeature.PLACES]},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"role-feature-chips-{role.slug}", response.content.decode())
        self.assertIn("Places layer", response.content.decode())
        self.assertIn("roleSettingsSaved", response.headers.get("HX-Trigger", ""))

    def test_non_admin_is_forbidden(self) -> None:
        role = baker.make(SubscriptionRole, slug="explorer", features="")
        other: User = baker.make(User)
        client = Client()
        client.force_login(other)
        response = client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "role_features", "role_slug": role.slug, "features": [SiteFeature.AI]},
        )
        self.assertEqual(response.status_code, 403)


class DefaultFeaturesActionTests(TestCase):
    """POST action=default_features toggles which SiteFeature values everyone gets, subscribed or not."""

    def setUp(self) -> None:
        super().setUp()
        self.admin: User = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_selected_features_are_saved(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "default_features", "features": [SiteFeature.AI, SiteFeature.SEARCH]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteSettings.get_current().feature_set, {SiteFeature.AI, SiteFeature.SEARCH})

    def test_unchecking_every_box_clears_default_features(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "default_features"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteSettings.get_current().feature_set, set())

    def test_unknown_feature_values_are_ignored(self) -> None:
        response = self.client.post(_SUBSCRIPTIONS_URL, {"action": "default_features", "features": [SiteFeature.AI, "not_a_real_feature"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteSettings.get_current().feature_set, {SiteFeature.AI})

    def test_htmx_request_returns_oob_chip_fragment(self) -> None:
        response = self.client.post(
            _SUBSCRIPTIONS_URL,
            {"action": "default_features", "features": [SiteFeature.PLACES]},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("role-feature-chips-__default__", response.content.decode())
        self.assertIn("Places layer", response.content.decode())
        self.assertIn("roleSettingsSaved", response.headers.get("HX-Trigger", ""))

    def test_non_admin_is_forbidden(self) -> None:
        other: User = baker.make(User)
        client = Client()
        client.force_login(other)
        response = client.post(_SUBSCRIPTIONS_URL, {"action": "default_features", "features": [SiteFeature.AI]})
        self.assertEqual(response.status_code, 403)


class UserHasFeatureDefaultTests(TestCase):
    """user_has_feature() grants SiteSettings.default_features to everyone, even with no subscription."""

    def test_user_with_no_subscription_gets_no_features_by_default(self) -> None:
        from urbanlens.dashboard.models.subscriptions import user_has_feature

        baker.make(User)  # first user is auto-promoted to site admin; keep it off the subject
        user: User = baker.make(User)
        self.assertFalse(user_has_feature(user, SiteFeature.AI))

    def test_user_with_no_subscription_gets_site_default_features(self) -> None:
        from urbanlens.dashboard.models.subscriptions import user_has_feature

        baker.make(User)
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
        user: User = baker.make(User)
        self.assertTrue(user_has_feature(user, SiteFeature.AI))
        self.assertFalse(user_has_feature(user, SiteFeature.SEARCH))

    def test_subscribed_user_still_gets_default_features_on_top_of_role_features(self) -> None:
        from urbanlens.dashboard.models.subscriptions import grant_subscription, user_has_feature

        baker.make(User)
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.SEARCH)
        role = baker.make(SubscriptionRole, slug="explorer", features=SiteFeature.AI)
        user: User = baker.make(User)
        granter: User = baker.make(User)
        grant_subscription(user, role, granter, months=None)
        self.assertTrue(user_has_feature(user, SiteFeature.AI))
        self.assertTrue(user_has_feature(user, SiteFeature.SEARCH))
