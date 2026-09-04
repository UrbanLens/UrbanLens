"""Tests for the multiplayer-stall fixes (SpotGuessr audit finding #1).

Covers ``force_reveal_round`` (the stall-sweep primitive - can end a session
as ABANDONED), ``expire_round_timer`` (the round-timer primitive - never
abandons), ``end_session_now`` (the host's manual escape hatch),
``GameSessionQuerySet.stalled()``, and the Celery sweep task itself.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import count

from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameRound,
    GameSession,
    GameSessionStatus,
    Guess,
    PlayerModeRating,
    SpotGuessrMode,
)
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr.session import (
    GameConfig,
    SpotGuessrError,
    begin_session,
    end_session_now,
    expire_round_timer,
    force_reveal_round,
    get_or_create_round,
    join_session,
    start_multiplayer_session,
    start_solo_session,
    submit_guess,
)

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


def _pinned_photo_location(*profiles: Profile) -> Location:
    location = _make_location()
    for profile in profiles:
        baker.make(Pin, profile=profile, location=location)
    baker.make(
        Image,
        location=location,
        media_type=MediaKind.PHOTO,
        latitude=None,
        longitude=None,
        wiki=baker.make(Wiki, location=location),
    )
    return location


def _setup_two_player_game(total_rounds: int = 1):
    host = _make_profile()
    guest = _make_profile()
    _befriend(host, guest)
    location = _pinned_photo_location(host, guest)
    session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), [guest], total_rounds=total_rounds)
    join_session(session, guest)
    round_ = begin_session(session, host)
    assert round_ is not None
    return host, guest, location, session, round_


class ForceRevealRoundTests(TestCase):
    def test_a_partial_guess_is_revealed_and_only_the_guesser_is_rated(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)  # only host guesses; guest never does

        force_reveal_round(round_)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(Guess.objects.filter(round=round_).count(), 1)
        self.assertTrue(PlayerModeRating.objects.filter(profile=host, mode=SpotGuessrMode.PHOTOS).exists())
        self.assertFalse(PlayerModeRating.objects.filter(profile=guest, mode=SpotGuessrMode.PHOTOS).exists())
        # Only round configured - the session should complete normally, not be abandoned.
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

    def test_zero_guesses_abandons_the_session(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()

        force_reveal_round(round_)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, GameSessionStatus.ABANDONED)
        self.assertIsNotNone(session.ended_at)

    def test_is_idempotent_on_an_already_revealed_round(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)
        submit_guess(round_, guest, guess_point)  # completes the round normally
        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

        force_reveal_round(round_)  # must not raise, double-rate, or otherwise misbehave
        self.assertEqual(PlayerModeRating.objects.get(profile=host, mode=SpotGuessrMode.PHOTOS).games_played, 1)

    def test_advances_to_the_next_round_when_more_remain(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game(total_rounds=2)
        _pinned_photo_location(host, guest)

        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)
        force_reveal_round(round_)

        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.ACTIVE)
        self.assertEqual(GameRound.objects.filter(session=session).count(), 2)


class ExpireRoundTimerTests(TestCase):
    def test_zero_guesses_does_not_abandon_the_session(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()

        expire_round_timer(round_)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertNotEqual(session.status, GameSessionStatus.ABANDONED)
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

    def test_a_solo_session_with_no_guess_advances_instead_of_stalling(self) -> None:
        profile = _make_profile()
        _pinned_photo_location(profile)
        _pinned_photo_location(profile)

        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=2)
        round_ = get_or_create_round(session)
        assert round_ is not None

        expire_round_timer(round_)
        round_.refresh_from_db()
        self.assertIsNotNone(round_.revealed_at)

        next_round = get_or_create_round(session)
        assert next_round is not None
        self.assertNotEqual(next_round.pk, round_.pk)

    def test_is_idempotent_on_an_already_revealed_round(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)
        submit_guess(round_, guest, guess_point)

        expire_round_timer(round_)  # must not raise or re-rate anyone
        self.assertEqual(PlayerModeRating.objects.get(profile=host, mode=SpotGuessrMode.PHOTOS).games_played, 1)


class EndSessionNowTests(TestCase):
    def test_non_host_cannot_end_the_game(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        with pytest.raises(SpotGuessrError):
            end_session_now(session, guest)

    def test_host_can_end_a_lobby_before_it_even_starts(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), [guest])

        end_session_now(session, host)
        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

    def test_ending_reveals_the_in_flight_round_and_rates_whoever_guessed(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game(total_rounds=3)
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)  # guest never guesses

        end_session_now(session, host)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)
        self.assertTrue(PlayerModeRating.objects.filter(profile=host, mode=SpotGuessrMode.PHOTOS).exists())
        # A session the host deliberately ended is COMPLETED, never ABANDONED -
        # even if literally nobody had guessed the in-flight round.
        self.assertNotEqual(session.status, GameSessionStatus.ABANDONED)

    def test_cannot_end_an_already_completed_session(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        end_session_now(session, host)
        with pytest.raises(SpotGuessrError):
            end_session_now(session, host)


class GameSessionStalledQuerySetTests(TestCase):
    def test_an_old_unrevealed_round_makes_its_session_stalled(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        GameRound.objects.filter(pk=round_.pk).update(created=timezone.now() - timedelta(minutes=30))

        cutoff = timezone.now() - timedelta(minutes=10)
        self.assertIn(session.pk, GameSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))

    def test_a_fresh_round_is_not_stalled(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        cutoff = timezone.now() - timedelta(minutes=10)
        self.assertNotIn(session.pk, GameSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))

    def test_a_revealed_round_is_not_stalled_even_if_old(self) -> None:
        host, guest, location, session, round_ = _setup_two_player_game()
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)
        submit_guess(round_, guest, guess_point)
        GameRound.objects.filter(pk=round_.pk).update(created=timezone.now() - timedelta(minutes=30))

        cutoff = timezone.now() - timedelta(minutes=10)
        self.assertNotIn(session.pk, GameSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))

    def test_a_lobby_session_with_no_rounds_yet_is_never_stalled(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), [guest])

        cutoff = timezone.now()
        self.assertNotIn(session.pk, GameSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))


class SweepStalledSpotguessrSessionsTaskTests(TestCase):
    def test_force_reveals_a_stalled_round_and_reports_the_count(self) -> None:
        from urbanlens.dashboard.tasks import sweep_stalled_spotguessr_sessions

        host, guest, location, session, round_ = _setup_two_player_game()
        guess_point = Point(float(location.longitude), float(location.latitude), srid=4326)
        submit_guess(round_, host, guess_point)
        GameRound.objects.filter(pk=round_.pk).update(created=timezone.now() - timedelta(minutes=30))

        swept_count = sweep_stalled_spotguessr_sessions()
        round_.refresh_from_db()

        self.assertEqual(swept_count, 1)
        self.assertIsNotNone(round_.revealed_at)

    def test_leaves_a_fresh_round_alone(self) -> None:
        from urbanlens.dashboard.tasks import sweep_stalled_spotguessr_sessions

        host, guest, location, session, round_ = _setup_two_player_game()

        swept_count = sweep_stalled_spotguessr_sessions()
        round_.refresh_from_db()

        self.assertEqual(swept_count, 0)
        self.assertIsNone(round_.revealed_at)
