"""Tests for setup wizard app-title branding rules."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.setup import (
    app_title_is_reserved,
    app_title_name_suggestions,
    is_official_urbanlens_site,
    is_reserved_urbanlens_title,
    normalize_app_title,
    personalized_map_title,
    setup_app_title_value,
    urbanlens_title_notice,
)
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

FLAGSHIP_HOST = "flagship.example.com"
OTHER_HOST = "maps.example.com"
_FLAGSHIP_SETTING = "urbanlens.UrbanLens.settings.app.settings.official_site_host"


class AppTitleNormalizationTests(SimpleTestCase):
    """Reserved-name detection is case-insensitive and ignores punctuation."""

    def test_normalize_strips_spaces_and_symbols(self) -> None:
        self.assertEqual(normalize_app_title(" Urban - Lens! "), "urbanlens")

    def test_reserved_title_variants(self) -> None:
        for title in ("UrbanLens", "URBAN LENS", "urban-lens", "U r b a n L e n s"):
            self.assertTrue(is_reserved_urbanlens_title(title))

    def test_non_reserved_titles(self) -> None:
        self.assertFalse(is_reserved_urbanlens_title("Alex's Map"))
        self.assertFalse(is_reserved_urbanlens_title("My Urbex Atlas"))


class PersonalizedTitleTests(TestCase):
    """Default title suggestions use first name or username."""

    def test_uses_first_name_when_present(self) -> None:
        user = baker.make(User, username="founder", first_name="Alexandra")
        self.assertEqual(personalized_map_title(user), "Alexandra's Map")

    def test_falls_back_to_username(self) -> None:
        user = baker.make(User, username="founder", first_name="")
        self.assertEqual(personalized_map_title(user), "founder's Map")


@override_settings(ALLOWED_HOSTS=["testserver", FLAGSHIP_HOST, OTHER_HOST])
class SetupWizardAppTitleTests(TestCase):
    """Setup wizard enforces branding rules on hosts other than the configured flagship."""

    def setUp(self) -> None:
        flagship = mock.patch(_FLAGSHIP_SETTING, FLAGSHIP_HOST)
        flagship.start()
        self.addCleanup(flagship.stop)
        self.user: User = baker.make(User, username="founder", first_name="Alex")
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
        official = factory.get("/", HTTP_HOST=FLAGSHIP_HOST)
        unofficial = factory.get("/", HTTP_HOST=OTHER_HOST)
        self.assertTrue(is_official_urbanlens_site(official))
        self.assertFalse(is_official_urbanlens_site(unofficial))

    def test_flagship_host_match_ignores_case_and_www(self) -> None:
        request = RequestFactory().get("/", HTTP_HOST=f"WWW.{FLAGSHIP_HOST.upper()}")
        with mock.patch(_FLAGSHIP_SETTING, f"www.{FLAGSHIP_HOST}"):
            self.assertTrue(is_official_urbanlens_site(request))

    def test_no_flagship_configured_reserves_nothing(self) -> None:
        request = RequestFactory().get("/", HTTP_HOST=OTHER_HOST)
        with mock.patch(_FLAGSHIP_SETTING, ""):
            self.assertFalse(is_official_urbanlens_site(request))
            self.assertFalse(app_title_is_reserved(request))

    def test_setup_get_replaces_default_on_non_official_host(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST=OTHER_HOST)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["setup_app_title"], "Alex's Map")
        self.assertContains(response, "Alex's Map", html=True)
        settings = SiteSettings.get_current()
        self.assertEqual(settings.app_title, "Alex's Map")

    def test_save_title_rejects_urbanlens_on_non_official_host(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "save_title", "app_title": "Urban Lens"},
            HTTP_HOST=OTHER_HOST,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(FLAGSHIP_HOST, response.json()["error"])
        settings = SiteSettings.get_current()
        self.assertEqual(settings.app_title, "UrbanLens")

    def test_save_title_allows_urbanlens_on_flagship_host(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "save_title", "app_title": "UrbanLens"},
            HTTP_HOST=FLAGSHIP_HOST,
        )

        self.assertEqual(response.status_code, 204)
        settings = SiteSettings.get_current()
        self.assertEqual(settings.app_title, "UrbanLens")

    def test_save_title_allows_urbanlens_when_no_flagship_configured(self) -> None:
        with mock.patch(_FLAGSHIP_SETTING, ""):
            response = self.client.post(
                reverse("setup"),
                {"action": "save_title", "app_title": "UrbanLens"},
                HTTP_HOST=OTHER_HOST,
            )

        self.assertEqual(response.status_code, 204)

    def test_setup_get_hides_title_notice_on_flagship_host(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST=FLAGSHIP_HOST)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "title-reserved-notice", html=True)
        self.assertNotContains(response, "please choose a different name", html=True)

    def test_setup_get_hides_title_notice_when_no_flagship_configured(self) -> None:
        with mock.patch(_FLAGSHIP_SETTING, ""):
            response = self.client.get(reverse("setup"), HTTP_HOST=OTHER_HOST)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "title-reserved-notice", html=True)
        self.assertNotContains(response, "please choose a different name", html=True)
        self.assertContains(response, 'data-official="true"')

    def test_setup_get_shows_title_notice_element_on_non_official_host(self) -> None:
        response = self.client.get(reverse("setup"), HTTP_HOST=OTHER_HOST)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="title-reserved-notice" hidden')
        self.assertContains(response, "please choose a different name")
        self.assertContains(response, FLAGSHIP_HOST)
        self.assertContains(response, '<p class="setup-title-notice__message" id="title-reserved-message"></p>')

    def test_complete_rejects_urbanlens_on_non_official_host(self) -> None:
        response = self.client.post(
            reverse("setup"),
            {"action": "complete"},
            HTTP_HOST=OTHER_HOST,
        )

        self.assertEqual(response.status_code, 400)
        settings = SiteSettings.get_current()
        self.assertFalse(settings.bootstrap_admin_onboarding_complete)

    def test_setup_app_title_value_flagship_keeps_factory_default(self) -> None:
        request = RequestFactory().get("/", HTTP_HOST=FLAGSHIP_HOST)
        value = setup_app_title_value(request, self.user, "UrbanLens")
        self.assertEqual(value, "UrbanLens")

    def test_setup_app_title_value_personalizes_reserved_name_off_flagship(self) -> None:
        request = RequestFactory().get("/", HTTP_HOST=OTHER_HOST)
        self.assertEqual(setup_app_title_value(request, self.user, "UrbanLens"), "Alex's Map")

    def test_urbanlens_title_notice_names_the_flagship_host(self) -> None:
        self.assertIn(FLAGSHIP_HOST, urbanlens_title_notice(FLAGSHIP_HOST))

    def test_name_suggestions_include_personalized_options(self) -> None:
        suggestions = app_title_name_suggestions(self.user)
        self.assertIn("Alex's Urbex Atlas", suggestions)
        self.assertEqual(len(suggestions), 5)
