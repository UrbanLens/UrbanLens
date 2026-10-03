"""Migration 0044's backfill: a departed row that guessed or answered counts as having left mid-game (P198)."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, GameSessionParticipant, Guess
from urbanlens.dashboard.models.trivia.model import TriviaAnswer, TriviaRound, TriviaSession, TriviaSessionParticipant

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0044_session_participant_departure")


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class SpotGuessrBackfillTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = baker.make(GameSession, host_profile=_profile())
        self.round = baker.make(GameRound, session=self.session, location=baker.make(Location), sequence_index=0)

    def _participant(self, status: str, *, guessed: bool, departure: str = "") -> GameSessionParticipant:
        participant = baker.make(
            GameSessionParticipant, session=self.session, profile=_profile(), status=status, departure=departure
        )
        if guessed:
            baker.make(Guess, round=self.round, profile=participant.profile, guess_point=Point(0, 0, srid=4326))
        return participant

    def _departure_after_backfill(self, participant: GameSessionParticipant) -> str:
        _MIGRATION._backfill_departures(apps, None)
        participant.refresh_from_db()
        return participant.departure

    def test_a_departed_player_who_guessed_left_mid_game(self) -> None:
        self.assertEqual(self._departure_after_backfill(self._participant("left", guessed=True)), "left")

    def test_a_departed_row_with_no_guess_stays_blank(self) -> None:
        self.assertEqual(self._departure_after_backfill(self._participant("left", guessed=False)), "")

    def test_a_player_still_joined_stays_blank(self) -> None:
        self.assertEqual(self._departure_after_backfill(self._participant("joined", guessed=True)), "")

    def test_a_recorded_removal_is_kept(self) -> None:
        participant = self._participant("left", guessed=True, departure="removed")
        self.assertEqual(self._departure_after_backfill(participant), "removed")

    def test_a_guess_in_another_session_does_not_count(self) -> None:
        participant = self._participant("left", guessed=False)
        other_session = baker.make(GameSession, host_profile=_profile())
        other_round = baker.make(GameRound, session=other_session, location=baker.make(Location), sequence_index=0)
        baker.make(Guess, round=other_round, profile=participant.profile, guess_point=Point(0, 0, srid=4326))
        self.assertEqual(self._departure_after_backfill(participant), "")


class TriviaBackfillTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = baker.make(TriviaSession, host_profile=_profile())
        self.round = baker.make(TriviaRound, session=self.session, sequence_index=0)

    def test_a_departed_player_who_answered_left_mid_game(self) -> None:
        participant = baker.make(TriviaSessionParticipant, session=self.session, profile=_profile(), status="left")
        baker.make(TriviaAnswer, round=self.round, profile=participant.profile)

        _MIGRATION._backfill_departures(apps, None)

        participant.refresh_from_db()
        self.assertEqual(participant.departure, "left")

    def test_a_departed_row_with_no_answer_stays_blank(self) -> None:
        participant = baker.make(TriviaSessionParticipant, session=self.session, profile=_profile(), status="left")

        _MIGRATION._backfill_departures(apps, None)

        participant.refresh_from_db()
        self.assertEqual(participant.departure, "")
