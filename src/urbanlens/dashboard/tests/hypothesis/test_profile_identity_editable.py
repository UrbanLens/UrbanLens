"""Tests for the profile hero's click-to-edit-in-place email/username/name.

Covers:
- Own-profile view renders the hero's email, username, and full-name as
  click-to-edit elements; other viewers and the Edit Profile page (which
  already has real form fields for all three) see plain text.
- ProfileFieldUpdateView's email/username/first_name/last_name POST paths -
  including the email format + uniqueness validation and the username
  format + availability validation those branches perform.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice


def _strip_scripts(html: str) -> str:
    """Remove <script> blocks so substring checks can't false-positive on the
    wiring script's own inert HTML-shaped string literals (its renderText()
    closures build strings like '<span ... data-raw-username="...' as JS
    text on every render, regardless of viewer)."""
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL)


class ProfileIdentityEditableRenderingTests(TestCase):
    """Own-profile view only: email/username/name render as click-to-edit."""

    def setUp(self) -> None:
        self.user = baker.make(User, username="urbex_jane", email="jane@example.com", first_name="Jane", last_name="Doe")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get_own(self):
        return self.client.get(reverse("profile.view"))

    def test_name_is_marked_editable_with_raw_parts(self) -> None:
        response = self._get_own()
        self.assertContains(response, "profile-name-editable")
        self.assertContains(response, 'data-raw-first="Jane"')
        self.assertContains(response, 'data-raw-last="Doe"')

    def test_username_is_marked_editable(self) -> None:
        response = self._get_own()
        self.assertContains(response, "profile-username-editable")
        self.assertContains(response, 'data-raw-username="urbex_jane"')

    def test_email_is_marked_editable(self) -> None:
        response = self._get_own()
        self.assertContains(response, "profile-email-editable")
        self.assertContains(response, 'data-raw-email="jane@example.com"')

    def test_other_viewer_sees_plain_identity_not_editable(self) -> None:
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        content = _strip_scripts(response.content.decode())
        self.assertIn("Jane Doe", content)
        self.assertNotIn('class="profile-name-editable"', content)
        self.assertNotIn("data-raw-username=", content)
        self.assertNotIn("data-raw-email=", content)

    def test_edit_profile_page_keeps_plain_text_not_the_inline_editor(self) -> None:
        """Edit Profile already has real form inputs for all three - the
        inline affordance must not double up there."""
        response = self.client.get(reverse("profile.edit"))
        content = _strip_scripts(response.content.decode())
        self.assertNotIn('class="profile-name-editable"', content)
        self.assertNotIn("profile-username-editable", content)
        self.assertNotIn("profile-email-editable", content)


class ProfileFieldUpdateIdentityTests(TestCase):
    """ProfileFieldUpdateView's email/username/name field autosave paths."""

    def setUp(self) -> None:
        self.user = baker.make(User, username="urbex_jane", email="jane@example.com")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, field: str, value: str):
        return self.client.post(reverse("profile.field.update"), {"field": field, "value": value})

    def test_updates_email(self) -> None:
        response = self._post("email", "new@example.com")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com")

    def test_invalid_email_rejected(self) -> None:
        response = self._post("email", "not-an-email")
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "jane@example.com")

    def test_duplicate_email_rejected(self) -> None:
        baker.make(User, email="taken@example.com")
        response = self._post("email", "taken@example.com")
        self.assertEqual(response.status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "jane@example.com")

    def test_updates_username(self) -> None:
        response = self._post("username", "new_handle")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "new_handle")

    def test_invalid_username_rejected(self) -> None:
        response = self._post("username", "x")
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "urbex_jane")

    def test_taken_username_rejected(self) -> None:
        baker.make(User, username="already_taken")
        response = self._post("username", "already_taken")
        self.assertEqual(response.status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "urbex_jane")

    def test_username_availability_check_get(self) -> None:
        baker.make(User, username="already_taken")
        response = self.client.get(reverse("profile.field.update"), {"field": "username", "value": "already_taken"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])

        response = self.client.get(reverse("profile.field.update"), {"field": "username", "value": "free_handle"})
        self.assertTrue(response.json()["available"])

    def test_updates_first_and_last_name(self) -> None:
        response = self._post("first_name", "Jane")
        self.assertEqual(response.status_code, 200)
        response = self._post("last_name", "Doe")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Jane")
        self.assertEqual(self.user.last_name, "Doe")
