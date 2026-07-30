"""Tests for services.spotguessr.selection's difficulty weighting.

Covers the proxy-seeding fix for the "difficulty slider is mostly a
placebo" audit finding: an unplayed (or barely-played) location must no
longer be scored as flatly neutral regardless of how well-documented it is.
"""

from __future__ import annotations

from itertools import count
import math

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import DEFAULT_RATING, GLICKO2_SCALE, LocationModeRating, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr.selection import (
    DIFFICULTY_BANDWIDTH,
    MAX_LOCATION_RATING,
    MIN_GAMES_FOR_DIFFICULTY_WEIGHTING,
    MIN_LOCATION_RATING,
    _difficulty_weight,
    _proxy_difficulty_rating,
    target_rating_for_difficulty,
)

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class ProxyDifficultyRatingTests(TestCase):
    def test_a_location_with_no_pins_or_photos_is_exactly_neutral(self) -> None:
        location = _make_location()
        self.assertEqual(_proxy_difficulty_rating(location), DEFAULT_RATING)

    def test_a_location_saturated_with_pins_and_photos_reads_as_easiest(self) -> None:
        location = _make_location()
        for _ in range(25):
            baker.make(Pin, profile=_make_profile(), location=location)
        for _ in range(15):
            baker.make(Image, location=location, media_type=MediaKind.PHOTO)
        self.assertEqual(_proxy_difficulty_rating(location), MIN_LOCATION_RATING)

    def test_partial_documentation_lands_strictly_between_neutral_and_easiest(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=_make_profile(), location=location)
        rating = _proxy_difficulty_rating(location)
        self.assertLess(rating, DEFAULT_RATING)
        self.assertGreater(rating, MIN_LOCATION_RATING)

    def test_more_documentation_is_never_treated_as_harder_than_neutral(self) -> None:
        sparse = _make_location()
        popular = _make_location()
        for _ in range(10):
            baker.make(Pin, profile=_make_profile(), location=popular)
        self.assertLessEqual(_proxy_difficulty_rating(popular), _proxy_difficulty_rating(sparse))


class DifficultyWeightTests(TestCase):
    def test_an_unplayed_popular_location_is_favored_by_the_easy_slider(self) -> None:
        """Regression guard for the placebo bug: before proxy-seeding, an
        unplayed location was scored as flatly neutral (DEFAULT_RATING)
        regardless of the difficulty slider, so easy/medium/hard all
        weighted it about the same. A well-documented, never-played
        location must now score meaningfully higher on "easy" than a
        never-played, undocumented one."""
        popular = _make_location()
        for _ in range(25):
            baker.make(Pin, profile=_make_profile(), location=popular)
        obscure = _make_location()

        easy_target = target_rating_for_difficulty(0.0)
        popular_weight = _difficulty_weight(popular, None, easy_target)
        obscure_weight = _difficulty_weight(obscure, None, easy_target)
        self.assertGreater(popular_weight, obscure_weight)

    def test_the_hard_slider_favors_the_undocumented_location_over_the_popular_one(self) -> None:
        popular = _make_location()
        for _ in range(25):
            baker.make(Pin, profile=_make_profile(), location=popular)
        obscure = _make_location()

        hard_target = target_rating_for_difficulty(1.0)
        popular_weight = _difficulty_weight(popular, None, hard_target)
        obscure_weight = _difficulty_weight(obscure, None, hard_target)
        self.assertGreater(obscure_weight, popular_weight)

    def test_a_location_with_enough_earned_games_ignores_the_proxy_entirely(self) -> None:
        """Once real play history clears the threshold, the earned rating
        must be used outright - a popular location's proxy signal must not
        leak in and distort an earned (and possibly very different) rating."""
        location = _make_location()
        for _ in range(25):
            baker.make(Pin, profile=_make_profile(), location=location)
        rating = baker.make(LocationModeRating, location=location, mode=SpotGuessrMode.PHOTOS, games_played=MIN_GAMES_FOR_DIFFICULTY_WEIGHTING)
        target = target_rating_for_difficulty(0.5)

        expected = math.exp(-((rating.rating - target) ** 2) / (2 * DIFFICULTY_BANDWIDTH**2))
        self.assertAlmostEqual(_difficulty_weight(location, rating, target), expected)

    def test_partial_earned_history_blends_proxy_and_earned_ratings(self) -> None:
        """Below the threshold, location_rating must be the weighted average
        of the proxy estimate and the earned rating (weighted by how close
        games_played is to the threshold) - not a discontinuous jump to
        either extreme the instant any real history exists."""
        location = _make_location()
        for _ in range(25):
            baker.make(Pin, profile=_make_profile(), location=location)
        # An earned rating far from the proxy estimate (proxy ~= MIN_LOCATION_RATING
        # here, since the location is saturated with pins).
        games_played = 2
        earned_mu = (MAX_LOCATION_RATING - DEFAULT_RATING) / GLICKO2_SCALE
        rating = baker.make(LocationModeRating, location=location, mode=SpotGuessrMode.PHOTOS, games_played=games_played, mu=earned_mu)

        target = target_rating_for_difficulty(0.5)
        proxy_rating = _proxy_difficulty_rating(location)
        earned_weight = games_played / MIN_GAMES_FOR_DIFFICULTY_WEIGHTING
        expected_location_rating = proxy_rating * (1 - earned_weight) + rating.rating * earned_weight
        expected_weight = math.exp(-((expected_location_rating - target) ** 2) / (2 * DIFFICULTY_BANDWIDTH**2))

        self.assertAlmostEqual(_difficulty_weight(location, rating, target), expected_weight)
        # Sanity check the blend actually moved away from both pure endpoints.
        self.assertNotAlmostEqual(expected_location_rating, proxy_rating)
        self.assertNotAlmostEqual(expected_location_rating, rating.rating)
