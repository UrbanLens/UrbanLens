"""Tests for services.spotguessr.modes - the per-mode strategy registry.

Round generation (services.spotguessr.session), round serialization
(services.spotguessr.serializers), and the photo-feedback gate
(services.spotguessr.relevance) all read this registry instead of each
keeping their own copy of "which modes exist" / "which modes show imagery" -
these tests guard the registry's own contract directly.
"""

from __future__ import annotations

from itertools import count

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import SpotGuessrMode
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr import modes
from urbanlens.dashboard.services.spotguessr.session import GameConfig

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class GetStrategyTests(TestCase):
    def test_every_declared_mode_has_a_registered_strategy(self) -> None:
        for mode in SpotGuessrMode.values:
            self.assertIsNotNone(modes.get_strategy(mode))

    def test_an_unknown_mode_has_no_strategy(self) -> None:
        self.assertIsNone(modes.get_strategy("not_a_real_mode"))


class ShowsImageryTests(TestCase):
    def test_photos_and_street_view_show_imagery(self) -> None:
        self.assertTrue(modes.shows_imagery(SpotGuessrMode.PHOTOS))
        self.assertTrue(modes.shows_imagery(SpotGuessrMode.STREET_VIEW))

    def test_named_place_does_not_show_imagery(self) -> None:
        self.assertFalse(modes.shows_imagery(SpotGuessrMode.NAMED_PLACE))

    def test_an_unknown_mode_does_not_show_imagery(self) -> None:
        self.assertFalse(modes.shows_imagery("not_a_real_mode"))


class BuildRoundTests(TestCase):
    def test_photos_strategy_returns_none_without_a_usable_photo(self) -> None:
        location = _make_location()
        profile = _make_profile()
        strategy = modes.get_strategy(SpotGuessrMode.PHOTOS)
        assert strategy is not None
        self.assertIsNone(strategy.build_round(location, GameConfig(), [profile]))

    def test_photos_strategy_returns_content_with_a_usable_photo(self) -> None:
        location = _make_location()
        profile = _make_profile()
        baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=location))
        strategy = modes.get_strategy(SpotGuessrMode.PHOTOS)
        assert strategy is not None
        content = strategy.build_round(location, GameConfig(), [profile])
        assert content is not None
        self.assertIsNotNone(content.image)
        self.assertIsNone(content.display_text)

    def test_photos_strategy_allows_a_solo_players_own_pin_photo(self) -> None:
        location = _make_location()
        profile = _make_profile()
        pin = baker.make(Pin, profile=profile, location=location)
        image = baker.make(Image, location=location, pin=pin, profile=profile, media_type=MediaKind.PHOTO, wiki=None)
        strategy = modes.get_strategy(SpotGuessrMode.PHOTOS)
        assert strategy is not None
        content = strategy.build_round(location, GameConfig(), [profile])
        assert content is not None
        self.assertEqual(content.image, image)

    def test_photos_strategy_never_uses_a_pin_photo_with_multiple_participants(self) -> None:
        location = _make_location()
        profile = _make_profile()
        other_profile = _make_profile()
        pin = baker.make(Pin, profile=profile, location=location)
        baker.make(Image, location=location, pin=pin, profile=profile, media_type=MediaKind.PHOTO, wiki=None)
        strategy = modes.get_strategy(SpotGuessrMode.PHOTOS)
        assert strategy is not None
        self.assertIsNone(strategy.build_round(location, GameConfig(), [profile, other_profile]))

    def test_named_place_strategy_returns_none_without_a_name(self) -> None:
        location = _make_location()
        profile = _make_profile()
        strategy = modes.get_strategy(SpotGuessrMode.NAMED_PLACE)
        assert strategy is not None
        self.assertIsNone(strategy.build_round(location, GameConfig(), [profile]))

    def test_named_place_strategy_returns_content_with_a_wiki_name(self) -> None:
        location = _make_location()
        profile = _make_profile()
        baker.make(Wiki, location=location, name="Old Mill House")
        strategy = modes.get_strategy(SpotGuessrMode.NAMED_PLACE)
        assert strategy is not None
        content = strategy.build_round(location, GameConfig(), [profile])
        assert content is not None
        self.assertIsNone(content.image)
        self.assertIsNotNone(content.display_text)
