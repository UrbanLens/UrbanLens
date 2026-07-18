"""Tests for the profile hero's click-to-edit-in-place area/started_exploring fields.

Covers:
- Own-profile view renders both hero meta fields as editable elements (even
  when empty, so there's something to click to add one) - other viewers, and
  the owner's own Edit Profile page (which already has full form fields for
  these), see plain text instead.
- ProfileFieldUpdateView's field="area"/field="started_exploring" POST paths,
  previously untested despite already existing (used by the full Edit
  Profile page) - now exercised more, via the profile view page's inline editor.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice


class ProfileHeroMetaEditableRenderingTests(TestCase):
    """Own-profile view (not Edit Profile) only: area/started_exploring render as click-to-edit."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get_own(self):
        return self.client.get(reverse("profile.view"))

    def _get_edit(self):
        return self.client.get(reverse("profile.edit"))

    def test_area_editable_markup_shown_with_a_value(self) -> None:
        self.profile.area = "Rochester, NY"
        self.profile.save(update_fields=["area"])
        response = self._get_own()
        self.assertContains(response, "profile-area--editable")
        self.assertContains(response, 'data-raw-area="Rochester, NY"')

    def test_area_placeholder_shown_with_no_value_yet(self) -> None:
        response = self._get_own()
        self.assertContains(response, "profile-area--editable")
        self.assertContains(response, "Add your area...")

    def test_started_exploring_editable_markup_shown_with_a_value(self) -> None:
        self.profile.started_exploring = "2015-06-01"
        self.profile.save(update_fields=["started_exploring"])
        response = self._get_own()
        self.assertContains(response, "profile-started-exploring--editable")
        self.assertContains(response, 'data-raw-started-exploring="2015-06-01"')

    def test_started_exploring_placeholder_shown_with_no_value_yet(self) -> None:
        response = self._get_own()
        self.assertContains(response, "profile-started-exploring--editable")
        self.assertContains(response, "Add when you started exploring...")

    def test_other_viewer_sees_plain_area_not_editable(self) -> None:
        self.profile.area = "Rochester, NY"
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["area", "profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        self.assertContains(response, "Rochester, NY")
        # The wiring script's `querySelector('.profile-area--editable')`
        # legitimately contains this class name as inert text on every render
        # (see the bio precedent's identical caveat) - check the actual
        # rendered element's class list, not just "does this string appear
        # anywhere in the page source".
        self.assertNotContains(response, 'profile-meta-item profile-area--editable"')

    def test_other_viewer_with_no_area_sees_nothing_not_a_placeholder(self) -> None:
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        # Same caveat: the wiring script hardcodes 'Add your area...' as its
        # revert-to-placeholder fallback string, present in every render
        # regardless of viewer - check for the text as actual element content.
        self.assertNotContains(response, ">Add your area...<")

    def test_edit_profile_page_keeps_the_plain_form_field_not_the_inline_editor(self) -> None:
        """The hero body is shared with Edit Profile, which already has a real
        <input name="area"> below it - the inline click-to-edit affordance
        must not also appear there and double up on the same field."""
        self.profile.area = "Rochester, NY"
        self.profile.save(update_fields=["area"])
        response = self._get_edit()
        self.assertContains(response, "Rochester, NY")
        self.assertNotContains(response, "profile-area--editable")
        self.assertNotContains(response, "profile-started-exploring--editable")


class ProfileFieldUpdateAreaAndStartedExploringTests(TestCase):
    """ProfileFieldUpdateView's field="area"/field="started_exploring" autosave paths."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, field: str, value: str):
        return self.client.post(reverse("profile.field.update"), {"field": field, "value": value})

    def test_updates_the_area(self) -> None:
        response = self._post("area", "Rochester, NY")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.area, "Rochester, NY")

    def test_clearing_area_sets_it_to_none(self) -> None:
        self.profile.area = "Old Area"
        self.profile.save(update_fields=["area"])
        response = self._post("area", "")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.area)

    def test_updates_started_exploring(self) -> None:
        response = self._post("started_exploring", "2015-06-01")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(str(self.profile.started_exploring), "2015-06-01")

    def test_invalid_started_exploring_date_is_rejected(self) -> None:
        response = self._post("started_exploring", "not-a-date")
        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.started_exploring)

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self._post("area", "Anonymous edit attempt")
        self.assertEqual(response.status_code, 302)
