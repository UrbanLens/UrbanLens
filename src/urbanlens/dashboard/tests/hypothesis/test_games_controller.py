"""Tests for controllers.games - the games hub landing page."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class GamesOverviewViewTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.games_url = reverse("games.overview")

    def test_requires_login(self) -> None:
        response = self.client.get(self.games_url)
        self.assertEqual(response.status_code, 302)

    def test_lists_spotguessr(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(self.games_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SpotGuessr")
        self.assertContains(response, reverse("spotguessr"))

    def test_nav_section_is_games_on_this_page(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(self.games_url)
        self.assertEqual(response.context["nav_section"], "games")

    def test_nav_section_is_also_games_while_playing_spotguessr(self) -> None:
        """The games hub's nav entry should stay highlighted while playing,
        not just on the hub page itself - see _NAV_SECTION_ALIASES."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("spotguessr"))
        self.assertEqual(response.context["nav_section"], "games")
