"""Tests for services.spotguessr.social.visible_friend_ratings - per-friend most-recently-played rating."""

from __future__ import annotations

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import PlayerModeRating, SpotGuessrMode, SpotGuessrPreference
from urbanlens.dashboard.services.spotguessr.social import visible_friend_ratings


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


class VisibleFriendRatingsTests(TestCase):
    def test_a_friend_who_only_played_named_place_still_shows_a_rating(self) -> None:
        """Regression guard: the lookup used to hardcode mode=photos, so a
        friend whose only session was Named Place or Street View appeared
        to have never played, even though their rating was updating fine
        in the database all along."""
        me = _make_profile()
        friend = _make_profile()
        _befriend(me, friend)
        baker.make(PlayerModeRating, profile=friend, mode=SpotGuessrMode.NAMED_PLACE, mu=0.5, last_played_at=timezone.now())

        visible = visible_friend_ratings(me)

        entry = next(e for e in visible if e["profile"].pk == friend.pk)
        assert entry["rating"] is not None
        self.assertEqual(entry["rating"].mode, SpotGuessrMode.NAMED_PLACE)

    def test_uses_each_friends_most_recently_played_mode_independently(self) -> None:
        me = _make_profile()
        friend = _make_profile()
        _befriend(me, friend)
        now = timezone.now()
        baker.make(PlayerModeRating, profile=friend, mode=SpotGuessrMode.PHOTOS, last_played_at=now - timezone.timedelta(days=1))
        baker.make(PlayerModeRating, profile=friend, mode=SpotGuessrMode.STREET_VIEW, last_played_at=now)

        visible = visible_friend_ratings(me)

        entry = next(e for e in visible if e["profile"].pk == friend.pk)
        self.assertEqual(entry["rating"].mode, SpotGuessrMode.STREET_VIEW)

    def test_a_friend_who_opted_out_is_excluded_regardless_of_mode(self) -> None:
        me = _make_profile()
        friend = _make_profile()
        _befriend(me, friend)
        baker.make(SpotGuessrPreference, profile=friend, show_ratings_to_friends=False)
        baker.make(PlayerModeRating, profile=friend, mode=SpotGuessrMode.NAMED_PLACE, last_played_at=timezone.now())

        visible = visible_friend_ratings(me)

        self.assertFalse(any(e["profile"].pk == friend.pk for e in visible))

    def test_a_friend_who_has_never_played_still_appears_with_no_rating(self) -> None:
        me = _make_profile()
        friend = _make_profile()
        _befriend(me, friend)

        visible = visible_friend_ratings(me)

        entry = next(e for e in visible if e["profile"].pk == friend.pk)
        self.assertIsNone(entry["rating"])
