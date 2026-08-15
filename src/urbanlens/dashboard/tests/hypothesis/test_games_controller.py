"""Tests for controllers.games - the games hub landing page and the shared feature gate."""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription


class GamesOverviewViewTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.games_url = reverse("games.overview")
        role = baker.make(SubscriptionRole, features=SiteFeature.ALPHA_FEATURES)
        grant_subscription(self.user, role, self.user, None)

    def test_requires_login(self) -> None:
        response = self.client.get(self.games_url)
        self.assertEqual(response.status_code, 302)

    def test_requires_alpha_features(self) -> None:
        non_alpha_user = baker.make(User)
        self.client.force_login(non_alpha_user)
        response = self.client.get(self.games_url)
        self.assertEqual(response.status_code, 403)

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


class GameFeatureGateTests(TestCase):
    """``AlphaFeatureRequiredMixin`` must cover every game route, not just the hub.

    Regression guard for the gap where only ``GamesOverviewView`` checked
    ``SiteFeature.ALPHA_FEATURES`` and anyone with a URL could play the games
    directly.
    """

    #: (url_name, args) per game: its landing page plus one in-session route.
    #: In-session pks are dummies - the gate fires in ``dispatch()``, before
    #: any session lookup could 404.
    GATED_ROUTES: ClassVar[list[tuple[str, tuple[int, ...]]]] = [
        ("spotguessr", ()),
        ("spotguessr.guess", (1, 1)),
        ("trivia", ()),
        ("trivia.answer", (1, 1)),
        ("consensus", ()),
        ("consensus.round", (1,)),
    ]

    def setUp(self) -> None:
        # The first user is auto-promoted to site admin and passes every
        # feature check - measure a second, genuinely unentitled user.
        baker.make(User)
        self.user = baker.make(User)

    def test_a_logged_in_user_without_the_feature_gets_403(self) -> None:
        self.client.force_login(self.user)
        for url_name, args in self.GATED_ROUTES:
            with self.subTest(route=url_name):
                response = self.client.get(reverse(url_name, args=args))
                self.assertEqual(response.status_code, 403)

    def test_an_anonymous_user_still_gets_the_login_redirect(self) -> None:
        """The mixin sits after ``LoginRequiredMixin``, so anonymous visitors
        are redirected to log in rather than shown a bare 403."""
        for url_name, args in self.GATED_ROUTES:
            with self.subTest(route=url_name):
                response = self.client.get(reverse(url_name, args=args))
                self.assertEqual(response.status_code, 302)

    def test_a_granted_non_admin_user_can_open_each_game(self) -> None:
        grant_alpha_features(self.user)
        self.client.force_login(self.user)
        for url_name in ("spotguessr", "trivia", "consensus"):
            with self.subTest(route=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
