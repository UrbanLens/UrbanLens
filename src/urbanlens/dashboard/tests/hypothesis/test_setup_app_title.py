"""Tests for the setup wizard's app-title defaults."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.setup import personalized_map_title
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

HOST = "maps.example.com"


class PersonalizedTitleTests(TestCase):
    """Default title suggestions use first name or username."""

    def test_uses_first_name_when_present(self) -> None:
        user = baker.make(User, username="founder", first_name="Alexandra")
        self.assertEqual(personalized_map_title(user), "Alexandra's Map")

    def test_falls_back_to_username(self) -> None:
        user = baker.make(User, username="founder", first_name="")
        self.assertEqual(personalized_map_title(user), "founder's Map")


@override_settings(ALLOWED_HOSTS=["testserver", HOST])
class SetupWizardAppTitleTests(TestCase):
    """The setup wizard shows a personalized placeholder and saves the title the admin types."""

    def setUp(self) -> None:
        self.user: User = baker.make(User, username="founder", first_name="Alex")
        add_user_to_site_admin_group(self.user)
        settings = SiteSettings.get_current()
        settings.bootstrap_admin_user = self.user
        settings.bootstrap_admin_onboarding_complete = False
        settings.app_title = "UrbanLens"
        settings.save()
        self.client = Client()
        self.client.force_login(self.user)

    def test_setup_get_uses_personalized_placeholder(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST=HOST)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["suggested_app_title"], "Alex's Map")
        self.assertContains(response, 'placeholder="Alex&#x27;s Map"')

    def test_setup_get_leaves_saved_title_alone(self) -> None:
        self.client.get(reverse("setup"), HTTP_HOST=HOST)

        self.assertEqual(SiteSettings.get_current().app_title, "UrbanLens")

    def test_save_title_stores_the_typed_title(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "save_title", "app_title": "Rust Belt Atlas"},
            HTTP_HOST=HOST,
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(SiteSettings.get_current().app_title, "Rust Belt Atlas")
