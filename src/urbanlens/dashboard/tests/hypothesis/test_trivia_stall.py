"""Tests for Trivia's multiplayer stall/leave/kick handling.

Covers ``force_reveal_round`` (the stall-sweep primitive - can end a session
as ABANDONED), ``end_session_now`` (the host's manual escape hatch),
``leave_session``/``kick_participant`` (voluntary departure and host
removal - no SpotGuessr equivalent exists yet, this is new ground),
``TriviaSessionQuerySet.stalled()``, and the Celery sweep task itself.
Mirrors ``test_spotguessr_stall.py``'s shape for the stall-handling pieces.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import count

from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import (
    PlayerTriviaRating,
    TriviaQuestion,
    TriviaQuestionSource,
    TriviaRound,
    TriviaSession,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
    TriviaSessionStatus,
)
from urbanlens.dashboard.services.trivia.session import (
    TriviaConfig,
    TriviaError,
    begin_session,
    end_session_now,
    force_reveal_round,
    get_or_create_round,
    invite_to_session,
    join_session,
    kick_participant,
    leave_session,
    start_multiplayer_session,
    submit_answer,
)

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_question(location: Location) -> TriviaQuestion:
    return baker.make(TriviaQuestion, location=location, source=TriviaQuestionSource.DETERMINISTIC, answer="1937")


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


def _setup_two_player_game(total_rounds: int = 1):
    host = _make_profile()
    guest = _make_profile()
    _befriend(host, guest)
    location = _make_location()
    baker.make(Pin, profile=host, location=location)
    baker.make(Pin, profile=guest, location=location)
    question = _make_question(location)
    session = start_multiplayer_session(host, TriviaConfig(), [guest], total_rounds=total_rounds)
    join_session(session, guest)
    round_ = begin_session(session, host)
    assert round_ is not None
    return host, guest, question, session, round_


class ForceRevealRoundTests(TestCase):
    def test_a_partial_answer_is_revealed_and_only_the_answerer_is_rated(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        submit_answer(round_, host, "1937")  # only host answers; guest never does

        force_reveal_round(round_)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertTrue(PlayerTriviaRating.objects.filter(profile=host).exists())
        self.assertFalse(PlayerTriviaRating.objects.filter(profile=guest).exists())
        self.assertEqual(session.status, TriviaSessionStatus.COMPLETED)

    def test_zero_answers_abandons_the_session(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()

        force_reveal_round(round_)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, TriviaSessionStatus.ABANDONED)
        self.assertIsNotNone(session.ended_at)

    def test_is_idempotent_on_an_already_revealed_round(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        submit_answer(round_, host, "1937")
        submit_answer(round_, guest, "1937")  # completes the round normally
        session.refresh_from_db()
        self.assertEqual(session.status, TriviaSessionStatus.COMPLETED)

        force_reveal_round(round_)  # must not raise, double-rate, or otherwise misbehave
        self.assertEqual(PlayerTriviaRating.objects.get(profile=host).games_played, 1)

    def test_advances_to_the_next_round_when_more_remain(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game(total_rounds=2)
        second_location = _make_location()
        baker.make(Pin, profile=host, location=second_location)
        baker.make(Pin, profile=guest, location=second_location)
        _make_question(second_location)

        submit_answer(round_, host, "1937")
        force_reveal_round(round_)

        session.refresh_from_db()
        self.assertEqual(session.status, TriviaSessionStatus.ACTIVE)
        self.assertEqual(TriviaRound.objects.filter(session=session).count(), 2)


class EndSessionNowTests(TestCase):
    def test_non_host_cannot_end_the_game(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        with pytest.raises(TriviaError):
            end_session_now(session, guest)

    def test_host_can_end_a_lobby_before_it_even_starts(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, TriviaConfig(), [guest])

        end_session_now(session, host)
        session.refresh_from_db()
        self.assertEqual(session.status, TriviaSessionStatus.COMPLETED)

    def test_ending_reveals_the_in_flight_round_and_rates_whoever_answered(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game(total_rounds=3)
        submit_answer(round_, host, "1937")  # guest never answers

        end_session_now(session, host)
        round_.refresh_from_db()
        session.refresh_from_db()

        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, TriviaSessionStatus.COMPLETED)
        self.assertTrue(PlayerTriviaRating.objects.filter(profile=host).exists())
        self.assertNotEqual(session.status, TriviaSessionStatus.ABANDONED)

    def test_cannot_end_an_already_completed_session(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        end_session_now(session, host)
        with pytest.raises(TriviaError):
            end_session_now(session, host)


class TriviaSessionStalledQuerySetTests(TestCase):
    def test_an_old_unrevealed_round_makes_its_session_stalled(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        TriviaRound.objects.filter(pk=round_.pk).update(created=timezone.now() - timedelta(minutes=30))

        cutoff = timezone.now() - timedelta(minutes=10)
        self.assertIn(session.pk, TriviaSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))

    def test_a_fresh_round_is_not_stalled(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        cutoff = timezone.now() - timedelta(minutes=10)
        self.assertNotIn(session.pk, TriviaSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))

    def test_a_revealed_round_is_not_stalled_even_if_old(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        submit_answer(round_, host, "1937")
        submit_answer(round_, guest, "1937")
        TriviaRound.objects.filter(pk=round_.pk).update(created=timezone.now() - timedelta(minutes=30))

        cutoff = timezone.now() - timedelta(minutes=10)
        self.assertNotIn(session.pk, TriviaSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))

    def test_a_lobby_session_with_no_rounds_yet_is_never_stalled(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, TriviaConfig(), [guest])

        cutoff = timezone.now()
        self.assertNotIn(session.pk, TriviaSession.objects.stalled(cutoff=cutoff).values_list("pk", flat=True))


class SweepStalledTriviaSessionsTaskTests(TestCase):
    def test_force_reveals_a_stalled_round_and_reports_the_count(self) -> None:
        from urbanlens.dashboard.tasks import sweep_stalled_trivia_sessions

        host, guest, question, session, round_ = _setup_two_player_game()
        submit_answer(round_, host, "1937")
        TriviaRound.objects.filter(pk=round_.pk).update(created=timezone.now() - timedelta(minutes=30))

        swept_count = sweep_stalled_trivia_sessions()
        round_.refresh_from_db()

        self.assertEqual(swept_count, 1)
        self.assertIsNotNone(round_.revealed_at)

    def test_leaves_a_fresh_round_alone(self) -> None:
        from urbanlens.dashboard.tasks import sweep_stalled_trivia_sessions

        host, guest, question, session, round_ = _setup_two_player_game()

        swept_count = sweep_stalled_trivia_sessions()
        round_.refresh_from_db()

        self.assertEqual(swept_count, 0)
        self.assertIsNone(round_.revealed_at)


class LeaveSessionTests(TestCase):
    def test_a_joined_guest_can_leave_and_is_marked_left(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        leave_session(session, guest)
        participant = TriviaSessionParticipant.objects.get(session=session, profile=guest)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.LEFT)

    def test_left_participant_is_excluded_from_the_lobby_payload(self) -> None:
        from urbanlens.dashboard.services.trivia.serializers import serialize_session

        host, guest, question, session, round_ = _setup_two_player_game()
        leave_session(session, guest)
        payload = serialize_session(session)
        self.assertNotIn(guest.pk, [p["profile_id"] for p in payload["participants"]])

    def test_a_non_participant_cannot_leave(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        outsider = _make_profile()
        with pytest.raises(TriviaError):
            leave_session(session, outsider)

    def test_cannot_leave_an_already_completed_session(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        end_session_now(session, host)
        with pytest.raises(TriviaError):
            leave_session(session, guest)

    def test_an_invited_but_never_joined_profile_can_decline_by_leaving(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        never_joins = _make_profile()
        _befriend(host, guest)
        _befriend(host, never_joins)
        session = start_multiplayer_session(host, TriviaConfig(), [guest, never_joins])

        leave_session(session, never_joins)
        participant = TriviaSessionParticipant.objects.get(session=session, profile=never_joins)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.LEFT)

    def test_last_remaining_participant_leaving_abandons_the_session(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        leave_session(session, guest)
        leave_session(session, host)
        session.refresh_from_db()
        self.assertEqual(session.status, TriviaSessionStatus.ABANDONED)

    def test_host_leaving_transfers_host_to_the_remaining_participant(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        leave_session(session, host)
        session.refresh_from_db()
        self.assertEqual(session.host_profile_id, guest.pk)
        self.assertNotEqual(session.status, TriviaSessionStatus.ABANDONED)

    def test_leaving_completes_the_round_if_it_was_the_last_holdout(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game(total_rounds=1)
        submit_answer(round_, host, "1937")  # guest never answers, then leaves instead
        leave_session(session, guest)
        round_.refresh_from_db()
        session.refresh_from_db()
        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, TriviaSessionStatus.COMPLETED)

    def test_a_departed_profile_can_be_reinvited(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, TriviaConfig(), [guest])
        join_session(session, guest)
        leave_session(session, guest)

        invite_to_session(session, host, guest)
        participant = TriviaSessionParticipant.objects.get(session=session, profile=guest)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.INVITED)


class KickParticipantTests(TestCase):
    def test_host_can_kick_a_joined_participant(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        kick_participant(session, host, guest)
        participant = TriviaSessionParticipant.objects.get(session=session, profile=guest)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.LEFT)

    def test_non_host_cannot_kick(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        with pytest.raises(TriviaError):
            kick_participant(session, guest, host)

    def test_host_cannot_kick_themselves(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        with pytest.raises(TriviaError):
            kick_participant(session, host, host)

    def test_cannot_kick_a_non_participant(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game()
        outsider = _make_profile()
        with pytest.raises(TriviaError):
            kick_participant(session, host, outsider)

    def test_kicking_the_last_holdout_completes_the_round(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game(total_rounds=1)
        submit_answer(round_, host, "1937")
        kick_participant(session, host, guest)
        round_.refresh_from_db()
        session.refresh_from_db()
        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, TriviaSessionStatus.COMPLETED)

    def test_kicking_an_invited_but_not_joined_participant_cancels_the_invite(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        never_joins = _make_profile()
        _befriend(host, guest)
        _befriend(host, never_joins)
        session = start_multiplayer_session(host, TriviaConfig(), [guest, never_joins])

        kick_participant(session, host, never_joins)
        participant = TriviaSessionParticipant.objects.get(session=session, profile=never_joins)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.LEFT)

    def test_get_or_create_round_stops_requiring_a_kicked_participants_pins(self) -> None:
        host, guest, question, session, round_ = _setup_two_player_game(total_rounds=2)
        # Second location only pinned by host - would be ineligible while
        # guest is still a joined participant.
        second_location = _make_location()
        baker.make(Pin, profile=host, location=second_location)
        second_question = _make_question(second_location)

        submit_answer(round_, host, "1937")
        kick_participant(session, host, guest)  # completes round 1 and, going forward, drops guest's pin requirement

        next_round = get_or_create_round(session)
        assert next_round is not None
        self.assertEqual(next_round.question_id, second_question.pk)
