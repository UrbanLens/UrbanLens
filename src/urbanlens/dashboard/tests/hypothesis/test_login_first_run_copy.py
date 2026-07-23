"""Tests for UL-179: the login page shouldn't say "Welcome back" the very
first time anyone sees it on a fresh install.

Uses SiteSettings.bootstrap_admin_onboarding_complete rather than
User.objects.exists() - by the time a brand-new install's first user
reaches this page, their account already exists (registration happens
before login), so a plain "any users exist" check would already read False
before the user ever saw the "first run" copy it's meant to gate.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings


class LoginFirstRunCopyTests(TestCase):
    def test_fresh_install_shows_welcome_not_welcome_back(self) -> None:
        """Default SiteSettings row (bootstrap_admin_onboarding_complete=False,
        the field's own default) - the state before anyone has ever set up
        the site, including before the very first account is created."""
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Welcome to UrbanLens")
        self.assertNotContains(response, "Welcome back")

    def test_during_bootstrap_admin_onboarding_still_shows_welcome(self) -> None:
        """The window this fix actually targets: the first user has
        registered (and been promoted to bootstrap admin) but hasn't yet
        finished the setup wizard - exactly when they'd see this login page
        for the first time after creating their account."""
        user = baker.make(User)
        settings = SiteSettings.get_current()
        settings.bootstrap_admin_user = user
        settings.bootstrap_admin_onboarding_complete = False
        settings.save(update_fields=["bootstrap_admin_user", "bootstrap_admin_onboarding_complete"])

        response = self.client.get(reverse("login"))

        self.assertContains(response, "Welcome to UrbanLens")
        self.assertNotContains(response, "Welcome back")

    def test_after_onboarding_complete_shows_welcome_back(self) -> None:
        user = baker.make(User)
        settings = SiteSettings.get_current()
        settings.bootstrap_admin_user = user
        settings.bootstrap_admin_onboarding_complete = True
        settings.save(update_fields=["bootstrap_admin_user", "bootstrap_admin_onboarding_complete"])

        response = self.client.get(reverse("login"))

        self.assertContains(response, "Welcome back")
        self.assertNotContains(response, "Welcome to UrbanLens")
