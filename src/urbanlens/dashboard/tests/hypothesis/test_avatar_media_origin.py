"""The profile editor and the setup wizard tell the page where uploads are served from, so a new avatar can be drawn from there."""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

MEDIA_ORIGIN = "https://media.example.com"
APP_HOST = "app.example.com"

_ORIGIN_SETTINGS = {
    "UL_MEDIA_BASE_URL": MEDIA_ORIGIN,
    "SITE_URL": f"https://{APP_HOST}",
    "ALLOWED_HOSTS": ["media.example.com", APP_HOST, "testserver"],
}


class AvatarMediaOriginAttributeTests(TestCase):
    """``data-media-origin`` on ``.edit-profile-page`` and ``.setup-wizard`` is what ``avatarSrc`` allows besides the page's own origin."""

    def setUp(self) -> None:
        super().setUp()
        # The first user is promoted to site admin and is the one the setup wizard is for.
        self.founder: User = baker.make(User, username="founder")
        add_user_to_site_admin_group(self.founder)
        site = SiteSettings.get_current()
        site.bootstrap_admin_user = self.founder
        site.bootstrap_admin_onboarding_complete = False
        site.save()
        self.member: User = baker.make(User, username="member")

    def _root_tag(self, url_name: str, user: User, css_class: str) -> str:
        """The opening tag of the element the page's script reads its values from."""
        self.client.force_login(user)
        response = self.client.get(reverse(url_name))
        self.assertEqual(response.status_code, 200)
        tag = re.search(rf'<div class="{css_class}[^>]*>', response.content.decode())
        if tag is None:
            self.fail(f"no .{css_class} on the {url_name} page")
        return tag.group(0)

    def _profile_editor(self) -> str:
        return self._root_tag("profile.edit", self.member, "edit-profile-page")

    def _setup_wizard(self) -> str:
        return self._root_tag("setup", self.founder, "setup-wizard")

    @override_settings(**_ORIGIN_SETTINGS)
    def test_the_profile_editor_names_the_configured_media_origin(self) -> None:
        self.assertIn(f'data-media-origin="{MEDIA_ORIGIN}"', self._profile_editor())

    @override_settings(**_ORIGIN_SETTINGS)
    def test_the_setup_wizard_names_the_configured_media_origin(self) -> None:
        self.assertIn(f'data-media-origin="{MEDIA_ORIGIN}"', self._setup_wizard())

    @override_settings(UL_MEDIA_BASE_URL="")
    def test_with_no_media_origin_the_attribute_is_empty(self) -> None:
        self.assertIn('data-media-origin=""', self._profile_editor())
        self.assertIn('data-media-origin=""', self._setup_wizard())

    @override_settings(
        **{**_ORIGIN_SETTINGS, "UL_MEDIA_BASE_URL": 'https://media.example.com/"><script>alert(1)</script>'}
    )
    def test_the_attribute_is_escaped(self) -> None:
        for page in (self._profile_editor(), self._setup_wizard()):
            self.assertNotIn("<script>", page)
            self.assertIn(
                'data-media-origin="https://media.example.com/&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"', page
            )
