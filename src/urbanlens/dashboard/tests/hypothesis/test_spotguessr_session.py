"""Integration tests for services.spotguessr.session - the solo Photos-mode game loop."""

from __future__ import annotations

from itertools import count

from django.contrib.gis.geos import Point
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameSessionStatus,
    LocationModeRating,
    PlayerModeRating,
    SpotGuessrMode,
)
from urbanlens.dashboard.services.spotguessr.session import (
    GameConfig,
    SpotGuessrError,
    complete_session,
    get_or_create_round,
    session_summary,
    start_solo_session,
    submit_guess,
)

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class StartSoloSessionTests(TestCase):
    def test_only_photos_mode_is_implemented(self) -> None:
        profile = _make_profile()
        with pytest.raises(SpotGuessrError):
            start_solo_session(profile, SpotGuessrMode.NAMED_PLACE, GameConfig())

    def test_creates_a_single_participant_session(self) -> None:
        profile = _make_profile()
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)
        self.assertEqual(session.mode, SpotGuessrMode.PHOTOS)
        self.assertEqual(session.total_rounds, 3)
        self.assertEqual(list(session.participants.values_list("profile_id", flat=True)), [profile.pk])

    def test_round_count_is_clamped_to_the_configured_bounds(self) -> None:
        profile = _make_profile()
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=999)
        self.assertLessEqual(session.total_rounds, 20)


class GetOrCreateRoundTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()

    def test_no_eligible_locations_returns_none(self) -> None:
        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        self.assertIsNone(get_or_create_round(session))

    def test_pinned_location_without_a_photo_is_skipped(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.profile, location=location)
        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        self.assertIsNone(get_or_create_round(session))

    def test_pinned_location_with_a_photo_produces_a_round(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.profile, location=location)
        baker.make(Image, location=location, media_type=MediaKind.PHOTO)

        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        round_ = get_or_create_round(session)

        self.assertIsNotNone(round_)
        self.assertEqual(round_.location_id, location.pk)
        self.assertEqual(round_.sequence_index, 0)

    def test_the_same_unanswered_round_is_returned_on_repeat_calls(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.profile, location=location)
        baker.make(Image, location=location, media_type=MediaKind.PHOTO)

        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        first = get_or_create_round(session)
        second = get_or_create_round(session)
        self.assertEqual(first.pk, second.pk)


class SubmitGuessTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)
        self.session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=1)
        self.round_ = get_or_create_round(self.session)

    def test_guessing_inside_the_boundary_scores_full_points(self) -> None:
        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)
        guess = submit_guess(self.round_, self.profile, guess_point)
        self.assertEqual(guess.points, 5000)
        self.assertEqual(guess.distance_meters, 0.0)

    def test_a_second_guess_by_the_same_profile_is_rejected(self) -> None:
        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)
        submit_guess(self.round_, self.profile, guess_point)
        with pytest.raises(SpotGuessrError):
            submit_guess(self.round_, self.profile, guess_point)

    def test_guessing_completes_the_round_and_updates_ratings(self) -> None:
        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)
        submit_guess(self.round_, self.profile, guess_point)
        self.round_.refresh_from_db()

        self.assertIsNotNone(self.round_.revealed_at)
        player_rating = PlayerModeRating.objects.get(profile=self.profile, mode=SpotGuessrMode.PHOTOS)
        location_rating = LocationModeRating.objects.get(location=self.location, mode=SpotGuessrMode.PHOTOS)
        self.assertEqual(player_rating.games_played, 1)
        self.assertEqual(location_rating.games_played, 1)
        # A perfect guess is a "win" for the player - rating should rise above the default.
        self.assertGreater(player_rating.rating, 1500.0)

    def test_session_participant_total_points_is_updated(self) -> None:
        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)
        submit_guess(self.round_, self.profile, guess_point)
        participant = self.session.participants.get(profile=self.profile)
        self.assertEqual(participant.total_points, 5000)


class SessionSummaryTests(TestCase):
    def test_summary_reports_rounds_played_and_totals(self) -> None:
        profile = _make_profile()
        location = _make_location()
        baker.make(Pin, profile=profile, location=location)
        baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)

        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=1)
        round_ = get_or_create_round(session)
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, profile, guess_point)

        self.assertIsNone(get_or_create_round(session))
        complete_session(session)
        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

        summary = session_summary(session)
        self.assertEqual(summary["rounds_played"], 1)
        self.assertEqual(summary["participants"], [{"profile_id": profile.pk, "username": profile.username, "total_points": 5000}])
