"""Another member's profile shows what you have in common, and no row at all when that is nothing."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice


class TheInCommonRowTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.member = baker.make(User).profile
        self.member.profile_visibility = VisibilityChoice.ANYONE
        self.member.save(update_fields=["profile_visibility"])
        self.viewer = baker.make(User).profile
        self.client.force_login(self.viewer.user)

    def _page(self) -> str:
        url = reverse("profile.view_user", kwargs={"profile_slug": self.member.slug or self.member.ensure_slug()})
        return self.client.get(url).content.decode()

    def test_nothing_in_common_shows_no_row(self) -> None:
        html = self._page()
        self.assertNotIn("profile-stats-row", html)
        self.assertNotIn("Nothing in Common", html)

    def test_a_place_in_common_shows_the_row(self) -> None:
        shared = baker.make(Location, official_name="Shared mill")
        Pin.objects.create(profile=self.member, location=shared)
        Pin.objects.create(profile=self.viewer, location=shared)
        for profile in (self.member, self.viewer):
            profile.common_pins_visibility = VisibilityChoice.ANYONE
            profile.save(update_fields=["common_pins_visibility"])

        self.assertIn("profile-stats-row", self._page())
