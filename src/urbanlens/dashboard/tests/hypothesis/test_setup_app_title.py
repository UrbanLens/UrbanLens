"""Tests for setup wizard app-title branding rules."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.setup import (
    app_title_name_suggestions,
    is_official_urbanlens_site,
    is_reserved_urbanlens_title,
    normalize_app_title,
    personalized_map_title,
    setup_app_title_value,
)
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group


class AppTitleNormalizationTests(TestCase):
    """Reserved-name detection is case-insensitive and ignores punctuation."""

    def test_normalize_strips_spaces_and_symbols(self) -> None:
        self.assertEqual(normalize_app_title(" Urban - Lens! "), "urbanlens")

    def test_reserved_title_variants(self) -> None:
        for title in ("UrbanLens", "URBAN LENS", "urban-lens", "U r b a n L e n s"):
            self.assertTrue(is_reserved_urbanlens_title(title))

    def test_non_reserved_titles(self) -> None:
        self.assertFalse(is_reserved_urbanlens_title("Jess's Map"))
        self.assertFalse(is_reserved_urbanlens_title("My Urbex Atlas"))


class PersonalizedTitleTests(TestCase):
    """Default title suggestions use first name or username."""

    def test_uses_first_name_when_present(self) -> None:
        user = baker.make(User, username="founder", first_name="Jessica")
        self.assertEqual(personalized_map_title(user), "Jessica's Map")

    def test_falls_back_to_username(self) -> None:
        user = baker.make(User, username="founder", first_name="")
        self.assertEqual(personalized_map_title(user), "founder's Map")


@override_settings(ALLOWED_HOSTS=["testserver", "urbanlens.org", "maps.example.com"])
class SetupWizardAppTitleTests(TestCase):
    """Setup wizard enforces branding rules on non-official hosts."""

    def setUp(self) -> None:
        self.user: User = baker.make(User, username="founder", first_name="Jess")
        add_user_to_site_admin_group(self.user)
        settings = SiteSettings.get_current()
        settings.bootstrap_admin_user = self.user
        settings.bootstrap_admin_onboarding_complete = False
        settings.app_title = "UrbanLens"
        settings.save()
        self.client = Client()
        self.client.force_login(self.user)

    def test_official_host_detection(self) -> None:
        factory = RequestFactory()
        official = factory.get("/", HTTP_HOST="urbanlens.org")
        unofficial = factory.get("/", HTTP_HOST="maps.example.com")
        self.assertTrue(is_official_urbanlens_site(official))
        self.assertFalse(is_official_urbanlens_site(unofficial))

    def test_setup_get_replaces_default_on_non_official_host(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST="maps.example.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jess's Map")
        settings = SiteSettings.get_current()
        self.assertEqual(settings.app_title, "Jess's Map")

    def test_save_title_rejects_urbanlens_on_non_official_host(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "save_title", "app_title": "Urban Lens"},
            HTTP_HOST="maps.example.com",
        )

        self.assertEqual(response.status_code, 400)
        settings = SiteSettings.get_current()
        self.assertEqual(settings.app_title, "UrbanLens")

    def test_save_title_allows_urbanlens_on_official_host(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "save_title", "app_title": "UrbanLens"},
            HTTP_HOST="urbanlens.org",
        )

        self.assertEqual(response.status_code, 204)
        settings = SiteSettings.get_current()
        self.assertEqual(settings.app_title, "UrbanLens")

    def test_setup_get_hides_title_notice_on_official_host(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST="urbanlens.org")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "title-reserved-notice")
        self.assertNotContains(
            response,
            "please choose a different name",
        )

    def test_setup_get_shows_title_notice_element_on_non_official_host(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST="maps.example.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="title-reserved-notice" hidden')
        self.assertContains(response, "please choose a different name")
        self.assertContains(response, '<p class="setup-title-notice__message" id="title-reserved-message"></p>')

    def test_complete_rejects_urbanlens_on_non_official_host(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "complete"},
            HTTP_HOST="maps.example.com",
        )

        self.assertEqual(response.status_code, 400)
        settings = SiteSettings.get_current()
        self.assertFalse(settings.bootstrap_admin_onboarding_complete)

    def test_setup_app_title_value_official_keeps_factory_default(self) -> None:
        factory = RequestFactory()
        request = factory.get("/", HTTP_HOST="urbanlens.org")
        value = setup_app_title_value(request, self.user, "UrbanLens")
        self.assertEqual(value, "UrbanLens")

    def test_name_suggestions_include_personalized_options(self) -> None:
        suggestions = app_title_name_suggestions(self.user)
        self.assertIn("Jess's Urbex Atlas", suggestions)
        self.assertEqual(len(suggestions), 5)
