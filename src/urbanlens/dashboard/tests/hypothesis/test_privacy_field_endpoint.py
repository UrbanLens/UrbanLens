"""The profile page's privacy hints change one setting and leave the rest as they are in the database."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile


class PrivacyFieldEndpointTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _post(self, field: str, value: str):
        return self.client.post(reverse("settings.privacy_field", args=[field]), {"value": value})

    def test_changing_one_setting_keeps_one_changed_since_the_page_loaded(self) -> None:
        """Two hints on one page each carried a snapshot of the others, so the second save reverted the first."""
        Profile.objects.filter(pk=self.profile.pk).update(profile_visibility="friends", contact_visibility="anyone")
        response = self._post("contact_visibility", "no_one")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "value": "no_one", "display": "No one"})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.contact_visibility, "no_one")
        self.assertEqual(self.profile.profile_visibility, "friends")

    def test_an_invalid_value_is_refused(self) -> None:
        response = self._post("profile_visibility", "everyone-ever")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_only_privacy_settings_can_be_changed(self) -> None:
        self.assertEqual(self._post("community_enabled", "false").status_code, 404)
        self.assertEqual(self._post("email", "x@example.invalid").status_code, 404)

    def test_with_community_off_nothing_changes(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(community_enabled=False, profile_visibility="no_one")
        response = self._post("profile_visibility", "anyone")
        self.assertEqual(response.status_code, 409)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.profile_visibility, "no_one")

    def test_signed_out_is_refused(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(profile_visibility="friends")
        self.client.logout()
        response = self._post("profile_visibility", "anyone")
        self.assertNotEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.profile_visibility, "friends")
