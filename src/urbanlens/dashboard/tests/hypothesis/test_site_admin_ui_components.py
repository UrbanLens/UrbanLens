"""Tests for the development-only UI components showcase page."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import EnvironmentOverrideChoice, SiteSettings
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group

_UI_COMPONENTS_URL = reverse("site_admin_ui_components")


class SiteAdminUIComponentsAccessTests(TestCase):
    """UI components page requires site admin permission and development environment."""

    def setUp(self) -> None:
        super().setUp()
        self._dev = EnvironmentOverrideChoice.DEVELOPMENT
        self._prod = EnvironmentOverrideChoice.PRODUCTION

    def test_unauthenticated_user_is_redirected(self) -> None:
        response = Client().get(_UI_COMPONENTS_URL)
        self.assertEqual(response.status_code, 302)

    def test_regular_user_gets_403(self) -> None:
        baker.make(User)
        user: User = baker.make(User)
        SiteSettings.objects.filter(pk=1).update(environment_override=self._dev)
        client = Client()
        client.force_login(user)
        response = client.get(_UI_COMPONENTS_URL)
        self.assertEqual(response.status_code, 403)

    def test_site_admin_gets_403_in_production(self) -> None:
        user: User = baker.make(User)
        add_user_to_site_admin_group(user)
        SiteSettings.objects.filter(pk=1).update(environment_override=self._prod)
        client = Client()
        client.force_login(user)
        response = client.get(_UI_COMPONENTS_URL)
        self.assertEqual(response.status_code, 403)

    def test_site_admin_gets_200_in_development(self) -> None:
        user: User = baker.make(User)
        add_user_to_site_admin_group(user)
        SiteSettings.objects.filter(pk=1).update(environment_override=self._dev)
        client = Client()
        client.force_login(user)
        response = client.get(_UI_COMPONENTS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "UI Components")
