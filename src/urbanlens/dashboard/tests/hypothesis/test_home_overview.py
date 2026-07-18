"""Tests for the logged-in homepage dashboard.

The profile page's "My Private Activity" section moved to a new homepage
(``/dashboard/home/``, the authenticated landing page): stats grid plus
recent-activity strips, everything visible only to its owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class HomeOverviewPageTests(TestCase):
    """The homepage renders the private dashboard for the signed-in user."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_renders_the_private_activity_panel(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Private Activity")
        self.assertContains(response, "Only visible to you")

    def test_hero_greets_the_user(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "Welcome back")

    def test_anonymous_users_are_redirected_to_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_site_root_redirects_authenticated_users_to_the_homepage(self) -> None:
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("home.view"))

    def test_nav_bar_home_link_is_active_on_the_homepage(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, ">Home</a>")

    def test_recently_created_pins_strip_shows_own_pins(self) -> None:
        pin: Pin = baker.make("dashboard.Pin", profile=self.profile, name="Old Asylum")
        self.assertEqual(pin.name, "Old Asylum")
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "Recently created pins")
        self.assertContains(response, "Old Asylum")


class ProfilePageAfterMoveTests(TestCase):
    """The profile page no longer hosts the private activity section."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_own_profile_no_longer_shows_the_private_activity_panel(self) -> None:
        response = self.client.get(reverse("profile.view"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "My Private Activity")

    def test_own_profile_still_renders_its_other_sections(self) -> None:
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "About")
        self.assertContains(response, "Friends")
