"""Tests for the Trivia multiplayer lobby lifecycle: invite, join, begin, joined-only round/answer logic.

Mirrors test_spotguessr_multiplayer.py's shape and coverage exactly, adapted
to Trivia's question-based (rather than location-based) rounds.
"""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import (
    TriviaQuestion,
    TriviaQuestionSource,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
    TriviaSessionStatus,
)
from urbanlens.dashboard.services.trivia.session import (
    TriviaConfig,
    TriviaError,
    begin_session,
    get_or_create_round,
    invite_to_session,
    join_session,
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


class StartMultiplayerSessionTests(TestCase):
    def test_creates_a_lobby_with_host_joined_and_invitees_invited(self) -> None:
        host = _make_profile()
        guest1 = _make_profile()
        guest2 = _make_profile()
        _befriend(host, guest1)
        _befriend(host, guest2)

        session = start_multiplayer_session(host, TriviaConfig(), [guest1, guest2])

        self.assertEqual(session.status, TriviaSessionStatus.LOBBY)
        participants = {p.profile_id: p.status for p in session.participants.all()}
        self.assertEqual(participants[host.pk], TriviaSessionParticipantStatus.JOINED)
        self.assertEqual(participants[guest1.pk], TriviaSessionParticipantStatus.INVITED)
        self.assertEqual(participants[guest2.pk], TriviaSessionParticipantStatus.INVITED)

    def test_invitees_get_a_notification(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)

        start_multiplayer_session(host, TriviaConfig(), [guest])

        notification = NotificationLog.objects.get(profile=guest, notification_type=NotificationType.TRIVIA_INVITE)
        self.assertEqual(notification.source_profile_id, host.pk)


class InviteToSessionTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _befriend(self.host, self.guest)
        self.session = start_multiplayer_session(self.host, TriviaConfig(), [])

    def test_non_host_cannot_invite(self) -> None:
        with pytest.raises(TriviaError):
            invite_to_session(self.session, self.guest, self.guest)

    def test_cannot_invite_a_non_friend(self) -> None:
        stranger = _make_profile()
        with pytest.raises(TriviaError):
            invite_to_session(self.session, self.host, stranger)

    def test_cannot_invite_once_the_game_has_started(self) -> None:
        self.session.status = TriviaSessionStatus.ACTIVE
        self.session.save(update_fields=["status"])
        with pytest.raises(TriviaError):
            invite_to_session(self.session, self.host, self.guest)

    def test_inviting_twice_does_not_double_notify(self) -> None:
        invite_to_session(self.session, self.host, self.guest)
        invite_to_session(self.session, self.host, self.guest)
        self.assertEqual(NotificationLog.objects.filter(profile=self.guest, notification_type=NotificationType.TRIVIA_INVITE).count(), 1)
        self.assertEqual(TriviaSessionParticipant.objects.filter(session=self.session, profile=self.guest).count(), 1)


class JoinSessionTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _befriend(self.host, self.guest)
        self.session = start_multiplayer_session(self.host, TriviaConfig(), [self.guest])

    def test_uninvited_profile_cannot_join(self) -> None:
        outsider = _make_profile()
        with pytest.raises(TriviaError):
            join_session(self.session, outsider)

    def test_invited_profile_can_join(self) -> None:
        participant = join_session(self.session, self.guest)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.JOINED)

    def test_joining_twice_is_idempotent(self) -> None:
        join_session(self.session, self.guest)
        participant = join_session(self.session, self.guest)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.JOINED)

    def test_cannot_join_after_the_roster_is_locked(self) -> None:
        self.session.status = TriviaSessionStatus.ACTIVE
        self.session.save(update_fields=["status"])
        with pytest.raises(TriviaError):
            join_session(self.session, self.guest)

    def test_an_already_joined_profile_can_still_be_fetched_after_the_roster_locks(self) -> None:
        join_session(self.session, self.guest)
        self.session.status = TriviaSessionStatus.ACTIVE
        self.session.save(update_fields=["status"])
        participant = join_session(self.session, self.guest)
        self.assertEqual(participant.status, TriviaSessionParticipantStatus.JOINED)


class BeginSessionTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _befriend(self.host, self.guest)
        self.location = _make_location()
        baker.make(Pin, profile=self.host, location=self.location)
        baker.make(Pin, profile=self.guest, location=self.location)
        self.question = _make_question(self.location)
        self.session = start_multiplayer_session(self.host, TriviaConfig(), [self.guest])
        join_session(self.session, self.guest)

    def test_non_host_cannot_begin(self) -> None:
        with pytest.raises(TriviaError):
            begin_session(self.session, self.guest)

    def test_host_begins_the_game(self) -> None:
        round_ = begin_session(self.session, self.host)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TriviaSessionStatus.ACTIVE)
        assert round_ is not None
        self.assertEqual(round_.question_id, self.question.pk)

    def test_cannot_begin_twice(self) -> None:
        begin_session(self.session, self.host)
        with pytest.raises(TriviaError):
            begin_session(self.session, self.host)

    @patch("urbanlens.dashboard.services.trivia.realtime.broadcast")
    def test_beginning_broadcasts_session_started(self, mock_broadcast) -> None:
        begin_session(self.session, self.host)
        event_types = [call.args[1] for call in mock_broadcast.call_args_list]
        self.assertIn("session.started", event_types)


class JoinedOnlyRoundAndAnswerTests(TestCase):
    """An INVITED-but-not-JOINED participant must not gate eligibility or round completion."""

    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        self.never_joins = _make_profile()
        _befriend(self.host, self.guest)
        _befriend(self.host, self.never_joins)

        self.location = _make_location()
        baker.make(Pin, profile=self.host, location=self.location)
        baker.make(Pin, profile=self.guest, location=self.location)
        # Deliberately no pin for `never_joins` - if their pins were required,
        # this location would be ineligible and the round could never be created.
        self.question = _make_question(self.location)

        self.session = start_multiplayer_session(self.host, TriviaConfig(), [self.guest, self.never_joins])
        join_session(self.session, self.guest)
        round_ = begin_session(self.session, self.host)
        assert round_ is not None
        self.round_ = round_

    def test_round_is_created_despite_the_never_joined_invitee_having_no_pins(self) -> None:
        self.assertIsNotNone(self.round_)

    def test_round_completes_after_only_joined_participants_answer(self) -> None:
        submit_answer(self.round_, self.host, "1937")
        self.round_.refresh_from_db()
        self.assertIsNone(self.round_.revealed_at)  # guest (joined) hasn't answered yet

        submit_answer(self.round_, self.guest, "1937")
        self.round_.refresh_from_db()
        self.assertIsNotNone(self.round_.revealed_at)  # never_joins was never counted

    @patch("urbanlens.dashboard.services.trivia.realtime.broadcast")
    def test_answering_broadcasts_and_completes_the_session_when_out_of_rounds(self, mock_broadcast) -> None:
        self.session.total_rounds = 1
        self.session.save(update_fields=["total_rounds"])

        submit_answer(self.round_, self.host, "1937")
        submit_answer(self.round_, self.guest, "1937")

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TriviaSessionStatus.COMPLETED)
        event_types = [call.args[1] for call in mock_broadcast.call_args_list]
        self.assertIn("round.revealed", event_types)
        self.assertIn("session.completed", event_types)

    def test_get_or_create_round_ignores_never_joined_participant_for_eligibility(self) -> None:
        # A second location pinned only by host+guest (not never_joins) must
        # still be eligible for round 2 - never_joins's empty pin list must
        # never veto it.
        second_location = _make_location()
        baker.make(Pin, profile=self.host, location=second_location)
        baker.make(Pin, profile=self.guest, location=second_location)
        second_question = _make_question(second_location)

        submit_answer(self.round_, self.host, "1937")
        submit_answer(self.round_, self.guest, "1937")  # completes round 1, eagerly creates round 2

        next_round = get_or_create_round(self.session)
        assert next_round is not None
        self.assertEqual(next_round.question_id, second_question.pk)
