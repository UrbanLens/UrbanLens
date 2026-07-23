"""Regression coverage for surfacing Profile.tos_accepted_at on the Settings page.

The field was stored (set when a user accepts the Terms of Service) but never
shown anywhere in the UI - docs/PROBLEMS.md flagged it as the one Profile
field with no home in any template. Added a small read-only line in the
Account tab's new "Account Info" section.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class SettingsTosAcceptedDisplayTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_shows_the_acceptance_date_when_recorded(self) -> None:
        accepted_at = timezone.make_aware(datetime.datetime(2025, 3, 4, 12, 0, 0))
        self.user.profile.tos_accepted_at = accepted_at
        self.user.profile.save(update_fields=["tos_accepted_at"])

        response = self.client.get(reverse("settings.view"))

        self.assertContains(response, "Terms of Service accepted on")
        self.assertContains(response, "Mar 4, 2025")

    def test_shows_a_fallback_when_not_recorded(self) -> None:
        self.user.profile.tos_accepted_at = None
        self.user.profile.save(update_fields=["tos_accepted_at"])

        response = self.client.get(reverse("settings.view"))

        self.assertContains(response, "No Terms of Service acceptance on record.")
