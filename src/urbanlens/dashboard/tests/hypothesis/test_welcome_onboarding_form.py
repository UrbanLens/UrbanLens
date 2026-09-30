"""The /welcome/ form's markup: the category cards' reveal and the Terms gate need no page script."""

from __future__ import annotations

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


class WelcomePageMarkupTests(TestCase):
    def test_continue_is_gated_on_the_terms_box_in_markup(self) -> None:
        user = baker.make(User)
        Profile.objects.filter(user=user).update(welcome_onboarding_complete=False)
        self.client.force_login(user)
        html = self.client.get(reverse("onboarding.welcome")).content.decode()
        tos_id = WelcomeOnboardingForm()["tos_agreed"].id_for_label
        self.assertIn(f'id="onboarding-continue-btn" data-enabled-by="{tos_id}" disabled', html)
        self.assertNotIn("syncContinueBtn", html)
