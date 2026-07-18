"""Tests for the profile view page's embedded social-links add/remove UI.

The owner's Social section now embeds the same self-contained HTMX partial
the Edit Profile page uses (its posts target the absolute ``profile.edit``
URL and it re-renders itself in place), so links can be added/removed
without leaving the profile page. Other viewers keep the read-only list,
hidden entirely when empty.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.social_link.model import SocialLink


def _strip_scripts(html: str) -> str:
    """Remove <script> blocks so substring checks can't false-positive on
    inline wiring-script string literals (same precedent as the other
    profile-page test files)."""
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL)


class ProfileSocialInlineRenderingTests(TestCase):
    """Owner sees the CRUD partial; other viewers see read-only or nothing."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get_own(self):
        return self.client.get(reverse("profile.view"))

    def test_owner_with_no_links_still_sees_the_section_with_an_add_form(self) -> None:
        content = _strip_scripts(self._get_own().content.decode())
        self.assertIn("social-links-content", content)
        self.assertIn("edit-add-link-form", content)
        self.assertIn('name="link_input"', content)

    def test_owner_sees_existing_links_with_remove_buttons(self) -> None:
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="urbex_jane")
        content = _strip_scripts(self._get_own().content.decode())
        self.assertIn("urbex_jane", content)
        self.assertIn('value="remove_link"', content)

    def test_other_viewer_sees_read_only_links_without_the_add_form(self) -> None:
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="urbex_jane")
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        content = _strip_scripts(response.content.decode())
        self.assertIn("urbex_jane", content)
        self.assertNotIn("edit-add-link-form", content)
        self.assertNotIn('value="remove_link"', content)

    def test_other_viewer_with_no_links_sees_no_social_section(self) -> None:
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        content = _strip_scripts(response.content.decode())
        self.assertNotIn(">Social<", content)


class ProfileSocialInlineActionTests(TestCase):
    """The embedded partial's HTMX add/remove round-trips (via profile.edit)."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _hx_post(self, data: dict):
        return self.client.post(reverse("profile.edit"), data, HTTP_HX_REQUEST="true")

    def test_add_link_creates_the_row_and_rerenders_the_partial(self) -> None:
        response = self._hx_post({"action": "add_link", "link_input": "https://instagram.com/urbex_jane"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SocialLink.objects.filter(profile=self.profile, platform="instagram", handle="urbex_jane").exists())
        self.assertContains(response, "social-links-content")
        self.assertContains(response, "urbex_jane")

    def test_unrecognised_url_rerenders_with_an_error(self) -> None:
        # A plain non-URL string parses as a catch-all "website" link by
        # design - a blocked scheme is what genuinely fails to parse.
        response = self._hx_post({"action": "add_link", "link_input": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "recognise that URL")
        self.assertFalse(SocialLink.objects.filter(profile=self.profile).exists())

    def test_remove_link_deletes_the_row(self) -> None:
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="urbex_jane")
        response = self._hx_post({"action": "remove_link", "remove_platform": "instagram"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SocialLink.objects.filter(profile=self.profile).exists())

    def test_remove_link_ignores_unknown_platforms(self) -> None:
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="urbex_jane")
        response = self._hx_post({"action": "remove_link", "remove_platform": "not-a-platform"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SocialLink.objects.filter(profile=self.profile).exists())
