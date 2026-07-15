"""Regression guard for the edit-profile page hero avatar not refreshing.

The avatar-upload/gravatar/emoji widgets on the edit-profile page save via a
plain fetch() JSON call (not an HTMX swap), so nothing server-rendered can OOB
-update the page hero's own avatar <img> - updateAvatarPreview() (edit.html)
has to reach into the DOM and update it directly by id. This only guards the
id actually being present on the rendered page; the JS behavior itself isn't
exercised by these Python tests (no browser here) - see edit.html's
updateAvatarPreview() for the JS side of this fix.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class ProfileHeroAvatarIdTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)

    def test_edit_page_hero_avatar_has_a_stable_id(self) -> None:
        response = self.client.get(reverse("profile.edit"))
        self.assertContains(response, 'id="profile-hero-avatar"')

    def test_edit_page_avatar_placeholder_also_carries_the_id_when_no_avatar_set(self) -> None:
        self.user.profile.avatar = None
        self.user.profile.save(update_fields=["avatar"])
        response = self.client.get(reverse("profile.edit"))
        self.assertContains(response, 'id="profile-hero-avatar"')

    def test_index_page_hero_avatar_also_carries_the_id(self) -> None:
        """profile/index.html shares _profile_hero_body.html with edit.html -
        the id must be present there too, not just on the edit page."""
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, 'id="profile-hero-avatar"')
