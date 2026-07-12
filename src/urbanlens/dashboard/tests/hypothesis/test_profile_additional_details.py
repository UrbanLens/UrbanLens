"""Tests for the profile page's "Additional Details" section and privacy hints.

Covers the gap found in an audit of the FAQ/Values claim "any data we store
about you is visible on your own profile page": birth_date and secondary
emails were collected but never displayed anywhere. This adds a compact
own-profile-only section for them, plus a hover-reveal privacy hint icon next
to data governed by a VisibilityChoice setting.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.profile.meta import VisibilityChoice


class AdditionalDetailsSectionTests(TestCase):
    """Own-profile view only: birth date and secondary emails."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get(self):
        return self.client.get(reverse("profile.view"))

    def test_birth_date_shown_when_set(self) -> None:
        self.profile.birth_date = datetime.date(1990, 6, 15)
        self.profile.save(update_fields=["birth_date"])
        response = self._get()
        self.assertContains(response, "Birthday: June 15, 1990")

    def test_birth_date_omitted_when_unset(self) -> None:
        response = self._get()
        self.assertNotContains(response, "Birthday:")

    def test_secondary_email_shown(self) -> None:
        ProfileEmail.objects.create(profile=self.profile, email="alt@example.com", is_verified=True)
        response = self._get()
        self.assertContains(response, "alt@example.com")

    def test_unverified_secondary_email_labelled(self) -> None:
        ProfileEmail.objects.create(profile=self.profile, email="pending@example.com", is_verified=False)
        response = self._get()
        self.assertContains(response, "pending@example.com (unverified)")

    def test_section_omitted_when_nothing_to_show(self) -> None:
        response = self._get()
        self.assertNotContains(response, "Additional Details")

    def test_other_viewer_never_sees_section(self) -> None:
        self.profile.birth_date = datetime.date(1990, 6, 15)
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["birth_date", "profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)
        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        self.assertNotContains(response, "Birthday:")
        self.assertNotContains(response, "Additional Details")


class ProfilePrivacyHintTests(TestCase):
    """Own-profile view shows hover privacy hints naming the governing setting."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_own_profile_shows_privacy_hints(self) -> None:
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "ul-privacy-hint")
        self.assertContains(response, self.profile.get_profile_visibility_display())

    def test_other_viewer_sees_no_privacy_hints(self) -> None:
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)
        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        self.assertNotContains(response, "ul-privacy-hint")
