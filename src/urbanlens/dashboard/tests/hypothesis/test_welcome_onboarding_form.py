"""The /welcome/ form's markup: the category cards sit in a native accordion, and the Terms gate needs no page script."""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.onboarding_form import WelcomeOnboardingForm
from urbanlens.dashboard.models.profile.model import Profile


def _customize_checked(form: WelcomeOnboardingForm) -> bool:
    return " checked" in str(form["customize_features"])


class CustomizeToggleTests(SimpleTestCase):
    def test_a_fresh_form_starts_collapsed(self) -> None:
        self.assertFalse(_customize_checked(WelcomeOnboardingForm()))

    def test_a_rerender_with_a_category_off_keeps_the_cards_open(self) -> None:
        """The user switched a category off then collapsed the cards; a failed submit must not hide that choice."""
        form = WelcomeOnboardingForm(data={"community_enabled": "on", "external_apis_enabled": "on"})
        self.assertFalse(form.is_valid())
        self.assertTrue(_customize_checked(form))

    def test_a_rerender_with_every_category_on_keeps_the_users_toggle(self) -> None:
        on = {"history_enabled": "on", "community_enabled": "on", "external_apis_enabled": "on"}
        self.assertFalse(_customize_checked(WelcomeOnboardingForm(data=on)))
        self.assertTrue(_customize_checked(WelcomeOnboardingForm(data={**on, "customize_features": "on"})))


HISTORY_DESCRIPTION = "Your visit journal, and any location data you choose to upload."


class FeatureCopyTests(SimpleTestCase):
    def test_history_is_described_in_one_short_sentence(self) -> None:
        self.assertEqual(WelcomeOnboardingForm()["history_enabled"].help_text, HISTORY_DESCRIPTION)

    def test_the_accordion_is_named_for_what_the_user_wants(self) -> None:
        self.assertEqual(WelcomeOnboardingForm()["customize_features"].label, "I want to disable some features")

    def test_the_box_behind_the_accordion_is_never_shown(self) -> None:
        self.assertIn(" hidden", str(WelcomeOnboardingForm()["customize_features"]))

    def test_the_accordion_is_open_exactly_when_the_box_is_checked(self) -> None:
        on = {"history_enabled": "on", "community_enabled": "on", "external_apis_enabled": "on"}
        self.assertFalse(WelcomeOnboardingForm().customize_open)
        self.assertFalse(WelcomeOnboardingForm(data=on).customize_open)
        self.assertTrue(WelcomeOnboardingForm(data={**on, "customize_features": "on"}).customize_open)
        self.assertTrue(WelcomeOnboardingForm(data={"community_enabled": "on"}).customize_open)
        for open_form in (
            WelcomeOnboardingForm(data={**on, "customize_features": "on"}),
            WelcomeOnboardingForm(data={"community_enabled": "on"}),
        ):
            self.assertTrue(_customize_checked(open_form))


def _accordion_tag(html: str) -> str:
    match = re.search(r"<details\b[^>]*\bonboarding-accordion\b[^>]*>", html)
    assert match, "the welcome page has no accordion"
    return match.group(0)


class WelcomePageMarkupTests(TestCase):
    def test_continue_is_gated_on_the_terms_box_in_markup(self) -> None:
        user = baker.make(User)
        Profile.objects.filter(user=user).update(welcome_onboarding_complete=False)
        self.client.force_login(user)
        html = self.client.get(reverse("onboarding.welcome")).content.decode()
        tos_id = WelcomeOnboardingForm()["tos_agreed"].id_for_label
        self.assertIn(f'id="onboarding-continue-btn" data-enabled-by="{tos_id}" disabled', html)
        self.assertNotIn("syncContinueBtn", html)

    def _welcome(self, **data: str) -> str:
        user = baker.make(User)
        Profile.objects.filter(user=user).update(welcome_onboarding_complete=False)
        self.client.force_login(user)
        if data:
            return self.client.post(reverse("onboarding.welcome"), data).content.decode()
        return self.client.get(reverse("onboarding.welcome")).content.decode()

    def test_the_features_are_a_native_accordion_that_starts_collapsed(self) -> None:
        html = self._welcome()
        tag = _accordion_tag(html)
        self.assertNotRegex(tag, r"\sopen\b")
        self.assertIn('data-mirrors-open="id_customize_features"', tag)
        summary = re.search(r"<summary\b[^>]*>(.*?)</summary>", html, flags=re.DOTALL)
        assert summary
        self.assertIn("I want to disable some features", summary.group(1))
        self.assertIn('aria-hidden="true"', summary.group(1))

    def test_the_cards_are_inside_the_accordion_and_the_terms_are_not(self) -> None:
        html = self._welcome()
        accordion = html[html.index("<details") : html.index("</details>")]
        self.assertIn("onboarding-card--history", accordion)
        self.assertIn("onboarding-card--external", accordion)
        self.assertNotIn("onboarding-tos", accordion)

    def test_the_checkbox_the_server_reads_is_hidden_and_unchecked(self) -> None:
        html = self._welcome()
        box = re.search(r'<input\b[^>]*\bname="customize_features"[^>]*>', html)
        assert box
        self.assertIn(" hidden", box.group(0))
        self.assertNotIn("checked", box.group(0))
        self.assertIn('type="checkbox"', box.group(0))

    def test_the_old_switch_is_gone(self) -> None:
        html = self._welcome()
        self.assertNotIn("Choose features to disable", html)
        self.assertNotIn("onboarding-customize", html)

    def test_the_history_card_carries_the_short_description(self) -> None:
        html = self._welcome()
        self.assertIn(f"{HISTORY_DESCRIPTION}</span>", html)
        self.assertNotIn("Disabling this will prevent you from uploading location data", html)
        self.assertNotIn("GPS route imports", html)

    def test_a_failed_submit_with_a_category_off_comes_back_expanded_and_checked(self) -> None:
        html = self._welcome(community_enabled="on", external_apis_enabled="on")
        self.assertRegex(_accordion_tag(html), r"\sopen\b")
        box = re.search(r'<input\b[^>]*\bname="customize_features"[^>]*>', html)
        assert box
        self.assertIn("checked", box.group(0))

    def test_a_failed_submit_with_everything_on_stays_collapsed(self) -> None:
        html = self._welcome(history_enabled="on", community_enabled="on", external_apis_enabled="on")
        self.assertNotRegex(_accordion_tag(html), r"\sopen\b")

    def test_an_expanded_accordion_posts_its_checkbox_as_on(self) -> None:
        user = baker.make(User)
        Profile.objects.filter(user=user).update(welcome_onboarding_complete=False)
        self.client.force_login(user)
        data = {
            "history_enabled": "on",
            "community_enabled": "on",
            "external_apis_enabled": "on",
            "tos_agreed": "on",
            "customize_features": "on",
        }
        self.client.post(reverse("onboarding.welcome"), data)
        profile = Profile.objects.get(user=user)
        self.assertTrue(profile.welcome_onboarding_complete)
        self.assertTrue(profile.track_pin_visits)
