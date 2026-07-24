"""Tests for the multiplayer lobby lifecycle (UL-392): invite, join, begin, joined-only round/guess logic."""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from django.contrib.gis.geos import Point
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameSessionParticipant,
    GameSessionParticipantStatus,
    GameSessionStatus,
    SpotGuessrMode,
)
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr.session import (
    GameConfig,
    SpotGuessrError,
    begin_session,
    get_or_create_round,
    invite_to_session,
    join_session,
    start_multiplayer_session,
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


class StartMultiplayerSessionTests(TestCase):
    def test_creates_a_lobby_with_host_joined_and_invitees_invited(self) -> None:
        host = _make_profile()
        guest1 = _make_profile()
        guest2 = _make_profile()
        _befriend(host, guest1)
        _befriend(host, guest2)

        session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), [guest1, guest2])

        self.assertEqual(session.status, GameSessionStatus.LOBBY)
        participants = {p.profile_id: p.status for p in session.participants.all()}
        self.assertEqual(participants[host.pk], GameSessionParticipantStatus.JOINED)
        self.assertEqual(participants[guest1.pk], GameSessionParticipantStatus.INVITED)
        self.assertEqual(participants[guest2.pk], GameSessionParticipantStatus.INVITED)

    def test_invitees_get_a_notification(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)

        start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), [guest])

        notification = NotificationLog.objects.get(profile=guest, notification_type=NotificationType.SPOTGUESSR_INVITE)
        self.assertEqual(notification.source_profile_id, host.pk)


class InviteToSessionTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _befriend(self.host, self.guest)
        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [])

    def test_non_host_cannot_invite(self) -> None:
        with pytest.raises(SpotGuessrError):
            invite_to_session(self.session, self.guest, self.guest)

    def test_cannot_invite_a_non_friend(self) -> None:
        stranger = _make_profile()
        with pytest.raises(SpotGuessrError):
            invite_to_session(self.session, self.host, stranger)

    def test_cannot_invite_once_the_game_has_started(self) -> None:
        self.session.status = GameSessionStatus.ACTIVE
        self.session.save(update_fields=["status"])
        with pytest.raises(SpotGuessrError):
            invite_to_session(self.session, self.host, self.guest)

    def test_inviting_twice_does_not_double_notify(self) -> None:
        invite_to_session(self.session, self.host, self.guest)
        invite_to_session(self.session, self.host, self.guest)
        self.assertEqual(NotificationLog.objects.filter(profile=self.guest, notification_type=NotificationType.SPOTGUESSR_INVITE).count(), 1)
        self.assertEqual(GameSessionParticipant.objects.filter(session=self.session, profile=self.guest).count(), 1)


class JoinSessionTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _befriend(self.host, self.guest)
        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest])

    def test_uninvited_profile_cannot_join(self) -> None:
        outsider = _make_profile()
        with pytest.raises(SpotGuessrError):
            join_session(self.session, outsider)

    def test_invited_profile_can_join(self) -> None:
        participant = join_session(self.session, self.guest)
        self.assertEqual(participant.status, GameSessionParticipantStatus.JOINED)

    def test_joining_twice_is_idempotent(self) -> None:
        join_session(self.session, self.guest)
        participant = join_session(self.session, self.guest)
        self.assertEqual(participant.status, GameSessionParticipantStatus.JOINED)

    def test_cannot_join_after_the_roster_is_locked(self) -> None:
        self.session.status = GameSessionStatus.ACTIVE
        self.session.save(update_fields=["status"])
        with pytest.raises(SpotGuessrError):
            join_session(self.session, self.guest)

    def test_an_already_joined_profile_can_still_be_fetched_after_the_roster_locks(self) -> None:
        join_session(self.session, self.guest)
        self.session.status = GameSessionStatus.ACTIVE
        self.session.save(update_fields=["status"])
        participant = join_session(self.session, self.guest)
        self.assertEqual(participant.status, GameSessionParticipantStatus.JOINED)


class BeginSessionTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _befriend(self.host, self.guest)
        self.location = _make_location()
        baker.make(Pin, profile=self.host, location=self.location)
        baker.make(Pin, profile=self.guest, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))
        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest])
        join_session(self.session, self.guest)

    def test_non_host_cannot_begin(self) -> None:
        with pytest.raises(SpotGuessrError):
            begin_session(self.session, self.guest)

    def test_host_begins_the_game(self) -> None:
        round_ = begin_session(self.session, self.host)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, GameSessionStatus.ACTIVE)
        assert round_ is not None
        self.assertEqual(round_.location_id, self.location.pk)

    def test_cannot_begin_twice(self) -> None:
        begin_session(self.session, self.host)
        with pytest.raises(SpotGuessrError):
            begin_session(self.session, self.host)

    @patch("urbanlens.dashboard.services.spotguessr.realtime.broadcast")
    def test_beginning_broadcasts_session_started(self, mock_broadcast) -> None:
        begin_session(self.session, self.host)
        event_types = [call.args[1] for call in mock_broadcast.call_args_list]
        self.assertIn("session.started", event_types)


class JoinedOnlyRoundAndGuessTests(TestCase):
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
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))

        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest, self.never_joins])
        join_session(self.session, self.guest)
        round_ = begin_session(self.session, self.host)
        assert round_ is not None
        self.round_ = round_

    def test_round_is_created_despite_the_never_joined_invitee_having_no_pins(self) -> None:
        self.assertIsNotNone(self.round_)

    def test_round_completes_after_only_joined_participants_guess(self) -> None:
        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)
        submit_guess(self.round_, self.host, guess_point)
        self.round_.refresh_from_db()
        self.assertIsNone(self.round_.revealed_at)  # guest (joined) hasn't guessed yet

        submit_guess(self.round_, self.guest, guess_point)
        self.round_.refresh_from_db()
        self.assertIsNotNone(self.round_.revealed_at)  # never_joins was never counted

    @patch("urbanlens.dashboard.services.spotguessr.realtime.broadcast")
    def test_guessing_broadcasts_and_completes_the_session_when_out_of_rounds(self, mock_broadcast) -> None:
        self.session.total_rounds = 1
        self.session.save(update_fields=["total_rounds"])
        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)

        submit_guess(self.round_, self.host, guess_point)
        submit_guess(self.round_, self.guest, guess_point)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, GameSessionStatus.COMPLETED)
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
        baker.make(Image, location=second_location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=second_location))

        guess_point = Point(float(self.location.longitude), float(self.location.latitude), srid=4326)
        submit_guess(self.round_, self.host, guess_point)
        submit_guess(self.round_, self.guest, guess_point)  # completes round 1, eagerly creates round 2

        next_round = get_or_create_round(self.session)
        assert next_round is not None
        self.assertEqual(next_round.location_id, second_location.pk)
