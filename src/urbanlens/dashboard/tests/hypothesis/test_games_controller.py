"""Tests for controllers.games - the games hub landing page and the shared feature gate."""

from __future__ import annotations

import re
from typing import ClassVar

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, reverse
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.inline_scripts import executable_blocks, inline_handlers, rendered_config
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
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

    Regression guard for the gap where only ``GamesOverviewView`` checked ``SiteFeature.ALPHA_FEATURES`` and
    anyone with a URL could play the games directly."""

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


def _alpha_profile(username: str) -> Profile:
    user = baker.make(User, username=username)
    grant_alpha_features(user)
    return Profile.objects.get(user=user)


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


class GameFriendPickerViewTests(TestCase):
    """The one invite picker every game loads its friend checkboxes from."""

    def setUp(self) -> None:
        baker.make(User)  # the first user is auto-promoted to site admin
        self.me = _alpha_profile("me")
        self.zed = _alpha_profile("zed")
        self.ana = _alpha_profile("Ana")
        self.stranger = _alpha_profile("stranger")
        _befriend(self.me, self.zed)
        _befriend(self.ana, self.me)
        self.url = reverse("games.friends")

    def _invitable_ids(self, query: str = "") -> list[str]:
        self.client.force_login(self.me.user)
        response = self.client.get(self.url + query)
        self.assertEqual(response.status_code, 200)
        return re.findall(r'name="invite_profile_ids" value="(\d+)"', response.content.decode())

    def test_requires_login(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_requires_alpha_features(self) -> None:
        self.client.force_login(baker.make(User))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_offers_only_friends_in_name_order(self) -> None:
        self.assertEqual(self._invitable_ids(), [str(self.ana.pk), str(self.zed.pk)])

    def test_draws_the_shared_checkbox_component(self) -> None:
        self.client.force_login(self.me.user)
        response = self.client.get(self.url)
        self.assertContains(response, 'class="ul-checkbox-wrap"', count=2)
        self.assertContains(response, "Ana")

    def test_exclude_drops_profiles_already_in_the_game(self) -> None:
        self.assertEqual(self._invitable_ids(f"?exclude={self.ana.pk},{self.stranger.pk}"), [str(self.zed.pk)])

    def test_malformed_exclude_tokens_are_ignored(self) -> None:
        self.assertEqual(self._invitable_ids(f"?exclude=abc,,-3,{self.zed.pk} ,1e3"), [str(self.ana.pk)])

    def test_no_one_left_to_invite_says_so(self) -> None:
        self.client.force_login(self.me.user)
        response = self.client.get(f"{self.url}?exclude={self.ana.pk},{self.zed.pk}")
        self.assertContains(response, "data-friend-picker-empty")
        self.assertNotContains(response, "invite_profile_ids")

    def test_the_per_game_json_endpoints_are_gone(self) -> None:
        for name in ("trivia.friends", "consensus.friends", "spotguessr.friends"):
            with self.subTest(route=name), self.assertRaises(NoReverseMatch):
                reverse(name)


class GamePagesRunNoInlineScriptTests(TestCase):
    """Each game's routes reach its bundle as a JSON island; the page itself runs nothing inline."""

    ISLANDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("spotguessr", "sg-urls"),
        ("trivia", "trivia-urls"),
        ("consensus", "consensus-urls"),
    )

    def setUp(self) -> None:
        user = baker.make(User)
        grant_alpha_features(user)
        self.client.force_login(user)

    def test_each_game_page_hands_its_routes_over_as_json(self) -> None:
        for url_name, island in self.ISLANDS:
            with self.subTest(game=url_name):
                content = self.client.get(reverse(url_name)).content
                urls = rendered_config(content, island)
                assert urls is not None
                self.assertEqual(urls["session_id_sentinel"], "999999999")
                self.assertIn("/999999999/", urls["lobby"])
                self.assertEqual(urls["friends"], reverse("games.friends"))
                self.assertEqual((executable_blocks(content), inline_handlers(content)), ([], []))

    def test_consensus_round_routes_carry_both_sentinels(self) -> None:
        urls = rendered_config(self.client.get(reverse("consensus")).content, "consensus-urls")
        assert urls is not None
        self.assertEqual(urls["round_id_sentinel"], "888888888")
        for key in ("answer", "skip", "vote", "photo"):
            with self.subTest(route=key):
                self.assertIn("/999999999/", urls[key])
                self.assertIn("/888888888/", urls[key])
