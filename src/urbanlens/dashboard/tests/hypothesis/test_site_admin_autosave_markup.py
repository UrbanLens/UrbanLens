"""Site admin's settings forms opt into ``form-autosave.ts`` from the site-admin bundle, not an inline script."""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile


class SiteAdminAutosaveMarkupTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        Profile.objects.filter(user=self.user).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(self.user)

    def test_every_settings_form_autosaves_from_the_bundle(self) -> None:
        html = self.client.get(reverse("site_admin")).content.decode()
        forms = re.findall(r"<form\b[^>]*\bclass=\"site-admin-form\"[^>]*>", html)
        self.assertGreater(len(forms), 1)
        self.assertTrue(all("data-autosave" in form for form in forms), forms)
        self.assertIn("dashboard/js/site-admin.js", html)
        self.assertNotIn("getCsrf", html)

    def test_a_clamped_save_reports_what_was_stored(self) -> None:
        response = self.client.post(
            reverse("site_admin"),
            {"max_trip_members": "-5"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("max_trip_members", payload["values"])
        self.assertGreaterEqual(payload["values"]["max_trip_members"], 0)
