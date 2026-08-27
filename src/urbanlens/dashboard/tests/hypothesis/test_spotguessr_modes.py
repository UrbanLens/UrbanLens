"""Tests for services.spotguessr.modes - the per-mode strategy registry.

Round generation (services.spotguessr.session), round serialization
(services.spotguessr.serializers), and the photo-feedback gate
(services.spotguessr.relevance) all read this registry instead of each
keeping their own copy of "which modes exist" / "which modes show imagery" -
these tests guard the registry's own contract directly.
"""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameRound, SpotGuessrMode
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr import modes
from urbanlens.dashboard.services.spotguessr.session import GameConfig, start_solo_session
from urbanlens.dashboard.services.spotguessr.street_view import StreetViewPanorama

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


class SerializeStreetViewTests(TestCase):
    """The one mode whose round payload deliberately includes the answer's
    coordinates - see StreetViewPanorama's docstring for why."""

    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        session = start_solo_session(self.profile, SpotGuessrMode.STREET_VIEW, GameConfig())
        self.round = GameRound.objects.create(session=session, sequence_index=0, location=self.location)
        self.strategy = modes.get_strategy(SpotGuessrMode.STREET_VIEW)
        assert self.strategy is not None

    @patch("urbanlens.dashboard.services.spotguessr.street_view.candidate_street_view_for_location")
    def test_includes_the_panoramas_own_coordinates_and_fallback_image(self, mock_candidate) -> None:
        mock_candidate.return_value = StreetViewPanorama(latitude=42.65, longitude=-73.76, image="data:image/jpeg;base64,abc123")
        data: dict = {}
        self.strategy.serialize_round(self.round, data)
        self.assertEqual(data["street_view_lat"], 42.65)
        self.assertEqual(data["street_view_lng"], -73.76)
        self.assertEqual(data["street_view_image"], "data:image/jpeg;base64,abc123")

    @patch("urbanlens.dashboard.services.spotguessr.street_view.candidate_street_view_for_location")
    def test_omits_every_field_when_coverage_is_no_longer_available(self, mock_candidate) -> None:
        # Coverage was confirmed once already at round-creation time (build_round),
        # but serialize_round re-fetches live on every call - a transient failure
        # here must degrade gracefully, not crash a round the player is mid-guessing.
        mock_candidate.return_value = None
        data: dict = {}
        self.strategy.serialize_round(self.round, data)
        self.assertNotIn("street_view_lat", data)
        self.assertNotIn("street_view_lng", data)
        self.assertNotIn("street_view_image", data)
