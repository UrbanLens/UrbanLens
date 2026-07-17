"""Tests for the profile page's click-to-edit-in-place bio.

Covers:
- Own-profile view renders the bio as an editable element (even with no bio
  yet, so there's something to click to add one) - other viewers see plain text.
- ProfileFieldUpdateView's field="bio" POST path, previously untested despite
  already existing (used by the full Edit Profile page) - now exercised more,
  via the profile view page's inline editor.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice


class ProfileBioEditableRenderingTests(TestCase):
    """Own-profile view only: the bio renders as a click-to-edit element."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get_own(self):
        return self.client.get(reverse("profile.view"))

    def test_editable_markup_shown_with_a_bio(self) -> None:
        self.profile.bio = "Urban explorer since 2015."
        self.profile.save(update_fields=["bio"])
        response = self._get_own()
        self.assertContains(response, "profile-bio-full--editable")
        self.assertContains(response, 'data-raw-bio="Urban explorer since 2015."')

    def test_about_section_shown_with_no_bio_yet(self) -> None:
        """Unlike the plain "hide if empty" behavior for other viewers, the
        owner needs the section rendered so there's something to click."""
        response = self._get_own()
        self.assertContains(response, "About")
        self.assertContains(response, "Add a bio...")

    def test_other_viewer_sees_plain_bio_not_editable(self) -> None:
        self.profile.bio = "Urban explorer since 2015."
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["bio", "profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        self.assertContains(response, "Urban explorer since 2015.")
        # The wiring script's `querySelector('.profile-bio-full--editable')`
        # legitimately contains this class name as inert text on every render
        # (it just no-ops when the element isn't there) - check the actual
        # rendered element's class list, not just "does this string appear
        # anywhere in the page source".
        self.assertNotContains(response, 'profile-bio-full profile-bio-full--editable"')

    def test_about_section_omitted_for_other_viewer_with_no_bio(self) -> None:
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        # Same caveat: the wiring script hardcodes 'Add a bio...' as its
        # revert-to-placeholder fallback string, present in every render
        # regardless of viewer - check for the text as actual element content.
        self.assertNotContains(response, ">Add a bio...<")


class ProfileFieldUpdateBioTests(TestCase):
    """ProfileFieldUpdateView's field="bio" autosave path."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, value: str):
        return self.client.post(reverse("profile.field.update"), {"field": "bio", "value": value})

    def test_updates_the_bio(self) -> None:
        response = self._post("New bio text.")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.bio, "New bio text.")

    def test_clearing_sets_it_to_none(self) -> None:
        self.profile.bio = "Old bio."
        self.profile.save(update_fields=["bio"])
        response = self._post("")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.bio)

    def test_requires_login(self) -> None:
        """LoginRequiredMixin redirects anonymous requests before the view's
        own manual is_authenticated check (a 401 JSON error) ever runs."""
        self.client.logout()
        response = self._post("Anonymous edit attempt.")
        self.assertEqual(response.status_code, 302)
