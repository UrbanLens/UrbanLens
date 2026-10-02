"""SpotGuessr leave, kick and lobby cancel: the service, its routes, and what a departed player can still reach."""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.gis.geos import Point
from django.test import TransactionTestCase
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.celery_inline import broadcasts_delivered_inline
from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import GameSessionConsumer
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameSession,
    GameSessionParticipant,
    GameSessionParticipantStatus,
    GameSessionStatus,
    Guess,
    SpotGuessrMode,
)
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr import overview, serializers
from urbanlens.dashboard.services.spotguessr.session import (
    CannotKickHostError,
    GameConfig,
    KickTargetNotAParticipantError,
    NotASessionParticipantError,
    NotSessionHostForKickError,
    ParticipantNotInvitedError,
    ParticipantNotJoinedError,
    SessionAlreadyEndedError,
    SessionAlreadyStartedError,
    begin_session,
    end_session_now,
    get_or_create_round,
    invite_to_session,
    join_session,
    kick_participant,
    leave_session,
    start_multiplayer_session,
    submit_guess,
)

_coordinate_counter = count()


def _make_profile() -> Profile:
    user = baker.make("auth.User")
    grant_alpha_features(user)
    return Profile.objects.get(user=user)


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


def _pinned_photo_location(*profiles: Profile) -> Location:
    offset = next(_coordinate_counter)
    location = baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")
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


def _point(location: Location) -> Point:
    return Point(float(location.longitude), float(location.latitude), srid=4326)


def _lobby(joined: int = 0, invited: int = 0):
    """A lobby with ``joined`` guests who accepted, then ``invited`` who have not."""
    host = _make_profile()
    guests = [_make_profile() for _ in range(joined + invited)]
    for guest in guests:
        _befriend(host, guest)
    session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), guests)
    for guest in guests[:joined]:
        join_session(session, guest)
    return host, guests, session


def _active_game(players: int = 2, total_rounds: int = 1):
    host = _make_profile()
    guests = [_make_profile() for _ in range(players - 1)]
    for guest in guests:
        _befriend(host, guest)
    locations = [_pinned_photo_location(host, *guests) for _ in range(total_rounds)]
    session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), guests, total_rounds=total_rounds)
    for guest in guests:
        join_session(session, guest)
    round_ = begin_session(session, host)
    assert round_ is not None
    return host, guests, locations, session, round_


def _status(session: GameSession, profile: Profile) -> str:
    return GameSessionParticipant.objects.get(session=session, profile=profile).status


class LeaveSessionTests(TestCase):
    def test_a_joined_guest_leaves_and_is_marked_left(self) -> None:
        _host, (guest,), _locations, session, _round = _active_game()

        leave_session(session, guest)

        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.LEFT)

    def test_an_invitee_declines_by_leaving(self) -> None:
        _host, (declines,), session = _lobby(invited=1)

        leave_session(session, declines)

        self.assertEqual(_status(session, declines), GameSessionParticipantStatus.LEFT)

    def test_a_departed_player_drops_out_of_the_lobby_payload(self) -> None:
        _host, (guest,), session = _lobby(1)

        leave_session(session, guest)

        self.assertNotIn(
            guest.pk, [row["profile_id"] for row in serializers.serialize_session(session)["participants"]]
        )

    def test_an_outsider_cannot_leave(self) -> None:
        _host, _guests, session = _lobby(1)

        with pytest.raises(NotASessionParticipantError):
            leave_session(session, _make_profile())

    def test_an_ended_session_cannot_be_left(self) -> None:
        host, (guest,), _locations, session, _round = _active_game()
        end_session_now(session, host)

        with pytest.raises(SessionAlreadyEndedError):
            leave_session(session, guest)

    def test_the_host_leaving_hands_the_game_to_the_earliest_remaining_player(self) -> None:
        host, (first, second), _locations, session, _round = _active_game(players=3)

        leave_session(session, host)

        session.refresh_from_db()
        self.assertEqual(session.host_profile_id, first.pk)
        self.assertEqual(session.status, GameSessionStatus.ACTIVE)
        self.assertEqual(_status(session, second), GameSessionParticipantStatus.JOINED)

    def test_the_last_player_leaving_abandons_the_session(self) -> None:
        host, (guest,), _locations, session, _round = _active_game()

        leave_session(session, guest)
        leave_session(session, host)

        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.ABANDONED)

    def test_the_last_holdout_leaving_reveals_the_round(self) -> None:
        host, (guest,), (location,), session, round_ = _active_game()
        submit_guess(round_, host, _point(location))

        leave_session(session, guest)

        round_.refresh_from_db()
        session.refresh_from_db()
        self.assertIsNotNone(round_.revealed_at)
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

    def test_a_departed_players_guess_does_not_stand_in_for_someone_still_playing(self) -> None:
        host, (leaver, still_playing), (location,), session, round_ = _active_game(players=3)
        submit_guess(round_, host, _point(location))
        submit_guess(round_, leaver, _point(location))

        leave_session(session, leaver)

        round_.refresh_from_db()
        self.assertIsNone(round_.revealed_at, "the round revealed before a player still in the game had guessed")

        submit_guess(round_, still_playing, _point(location))
        round_.refresh_from_db()
        self.assertIsNotNone(round_.revealed_at)

    def test_a_departed_player_cannot_guess(self) -> None:
        host, (leaver, _other), (location,), session, round_ = _active_game(players=3)
        leave_session(session, leaver)

        with pytest.raises(ParticipantNotJoinedError):
            submit_guess(round_, leaver, _point(location))

    def test_a_departed_player_needs_a_fresh_invite_to_rejoin(self) -> None:
        host, (guest,), session = _lobby(1)
        leave_session(session, guest)

        with pytest.raises(ParticipantNotInvitedError):
            join_session(session, guest)
        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.LEFT)

        invite_to_session(session, host, guest)
        join_session(session, guest)
        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.JOINED)

    def test_leaving_is_announced_with_any_new_host(self) -> None:
        host, (guest,), session = _lobby(1)

        with patch("urbanlens.dashboard.services.spotguessr.realtime.broadcast") as broadcast:
            leave_session(session, host)

        left = [call.args[2] for call in broadcast.call_args_list if call.args[1] == "participant.left"]
        self.assertEqual(left, [{"profile_id": host.pk, "reason": "left", "new_host_profile_id": guest.pk}])

    def test_a_departed_player_stops_constraining_eligibility(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        shared = _pinned_photo_location(host, guest)
        session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, GameConfig(), [guest], total_rounds=2)
        join_session(session, guest)
        round_ = begin_session(session, host)
        assert round_ is not None
        self.assertEqual(round_.location_id, shared.pk)
        # Pinned only by the host: ineligible while the guest still plays.
        host_only = _pinned_photo_location(host)
        submit_guess(round_, host, _point(shared))

        leave_session(session, guest)

        next_round = get_or_create_round(session)
        assert next_round is not None
        self.assertEqual(next_round.location_id, host_only.pk)


class KickParticipantTests(TestCase):
    def test_the_host_removes_a_joined_player(self) -> None:
        host, (guest,), _locations, session, _round = _active_game()

        kick_participant(session, host, guest)

        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.LEFT)

    def test_the_host_withdraws_an_unanswered_invite(self) -> None:
        host, (invitee,), session = _lobby(invited=1)

        kick_participant(session, host, invitee)

        self.assertEqual(_status(session, invitee), GameSessionParticipantStatus.LEFT)

    def test_a_player_who_is_not_the_host_is_refused(self) -> None:
        host, (guest, other), _locations, session, _round = _active_game(players=3)

        with pytest.raises(NotSessionHostForKickError):
            kick_participant(session, guest, other)
        self.assertEqual(_status(session, other), GameSessionParticipantStatus.JOINED)

    def test_a_host_who_has_since_left_cannot_kick(self) -> None:
        host, (_first, second), _locations, session, _round = _active_game(players=3)
        read_while_still_host = GameSession.objects.get(pk=session.pk)
        leave_session(session, host)

        with pytest.raises(NotSessionHostForKickError):
            kick_participant(read_while_still_host, host, second)
        self.assertEqual(_status(session, second), GameSessionParticipantStatus.JOINED)

    def test_the_host_cannot_remove_themselves(self) -> None:
        host, _guests, _locations, session, _round = _active_game()

        with pytest.raises(CannotKickHostError):
            kick_participant(session, host, host)

    def test_an_outsider_cannot_be_removed(self) -> None:
        host, _guests, _locations, session, _round = _active_game()

        with pytest.raises(KickTargetNotAParticipantError):
            kick_participant(session, host, _make_profile())

    def test_an_ended_session_refuses_a_kick(self) -> None:
        host, (guest,), _locations, session, _round = _active_game()
        end_session_now(session, host)

        with pytest.raises(SessionAlreadyEndedError):
            kick_participant(session, host, guest)

    def test_removing_the_last_holdout_reveals_the_round(self) -> None:
        host, (guest,), (location,), session, round_ = _active_game()
        submit_guess(round_, host, _point(location))

        kick_participant(session, host, guest)

        round_.refresh_from_db()
        self.assertIsNotNone(round_.revealed_at)

    def test_a_kick_landing_while_the_invitee_accepts_is_not_undone(self) -> None:
        host, (guest,), session = _lobby(invited=1)
        read_before_the_kick = GameSessionParticipant.objects.get(session=session, profile=guest)
        kick_participant(session, host, guest)

        with (
            patch.object(GameSessionParticipant.objects, "get", return_value=read_before_the_kick),
            pytest.raises(ParticipantNotInvitedError),
        ):
            join_session(session, guest)

        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.LEFT)

    def test_a_guess_from_a_player_kicked_after_its_check_is_refused(self) -> None:
        host, (guest,), (location,), session, round_ = _active_game()
        submit_guess(round_, host, _point(location))
        read_before_the_kick = GameSessionParticipant.objects.get(session=session, profile=guest)
        kick_participant(session, host, guest)

        with (
            patch.object(GameSessionParticipant.objects, "get", return_value=read_before_the_kick),
            pytest.raises(ParticipantNotJoinedError),
        ):
            submit_guess(round_, guest, _point(location))

        self.assertFalse(Guess.objects.filter(round=round_, profile=guest).exists())

    def test_a_stale_begin_cannot_revive_a_lobby_its_host_abandoned(self) -> None:
        host, _invitees, session = _lobby(invited=1)
        read_before_leaving = GameSession.objects.get(pk=session.pk)
        leave_session(session, host)

        with pytest.raises(SessionAlreadyStartedError):
            begin_session(read_before_leaving, host)

        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.ABANDONED)

    def test_a_kick_is_announced_as_one(self) -> None:
        host, (guest,), session = _lobby(1)

        with patch("urbanlens.dashboard.services.spotguessr.realtime.broadcast") as broadcast:
            kick_participant(session, host, guest)

        left = [call.args[2] for call in broadcast.call_args_list if call.args[1] == "participant.left"]
        self.assertEqual(left, [{"profile_id": guest.pk, "reason": "kicked", "new_host_profile_id": None}])


class SessionHistoryTests(TestCase):
    def test_a_session_the_player_left_is_not_in_their_history(self) -> None:
        _host, (guest,), _locations, session, _round = _active_game()
        self.assertIn(session.pk, overview.participated_sessions(guest).values_list("pk", flat=True))

        leave_session(session, guest)

        self.assertNotIn(session.pk, overview.participated_sessions(guest).values_list("pk", flat=True))


class LeaveKickRouteTests(TestCase):
    def _post(self, profile: Profile, name: str, session: GameSession, data: dict | None = None):
        self.client.force_login(profile.user)
        return self.client.post(reverse(name, kwargs={"session_id": session.pk}), data or {})

    def _get(self, profile: Profile, name: str, session: GameSession):
        self.client.force_login(profile.user)
        return self.client.get(reverse(name, kwargs={"session_id": session.pk}))

    def test_a_player_leaves_through_the_route(self) -> None:
        _host, (guest,), session = _lobby(1)

        response = self._post(guest, "spotguessr.leave", session)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"left": True})
        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.LEFT)

    def test_an_outsider_gets_a_404_from_leave(self) -> None:
        _host, _guests, session = _lobby(1)

        self.assertEqual(self._post(_make_profile(), "spotguessr.leave", session).status_code, 404)

    def test_the_host_kicks_through_the_route(self) -> None:
        host, (guest,), session = _lobby(1)

        response = self._post(host, "spotguessr.kick", session, {"profile_id": guest.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"kicked": True})
        self.assertEqual(_status(session, guest), GameSessionParticipantStatus.LEFT)

    def test_a_guest_cannot_kick_through_the_route(self) -> None:
        host, (guest, other), session = _lobby(2)

        response = self._post(guest, "spotguessr.kick", session, {"profile_id": other.pk})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Only the host can remove a player.")
        self.assertEqual(_status(session, other), GameSessionParticipantStatus.JOINED)

    def test_a_kick_without_a_profile_is_a_400(self) -> None:
        host, _guests, session = _lobby(1)

        self.assertEqual(self._post(host, "spotguessr.kick", session).status_code, 400)

    def test_the_host_cancels_the_lobby(self) -> None:
        host, (guest,), session = _lobby(1)

        response = self._post(host, "spotguessr.end", session)

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.COMPLETED)

    def test_a_guest_cannot_cancel_the_lobby(self) -> None:
        _host, (guest,), session = _lobby(1)

        response = self._post(guest, "spotguessr.end", session)

        self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.status, GameSessionStatus.LOBBY)

    def test_a_kicked_player_loses_every_session_route(self) -> None:
        host, (guest,), _locations, session, _round = _active_game()
        self.assertEqual(self._get(guest, "spotguessr.round", session).status_code, 200)

        kick_participant(session, host, guest)

        for name in ("spotguessr.round", "spotguessr.lobby", "spotguessr.chat_history", "spotguessr.summary"):
            with self.subTest(name=name):
                self.assertEqual(self._get(guest, name, session).status_code, 404)
        self.assertEqual(self._get(host, "spotguessr.lobby", session).status_code, 200)

    def test_the_deep_link_does_not_reopen_a_session_the_player_left(self) -> None:
        _host, (guest,), _locations, session, _round = _active_game()
        leave_session(session, guest)
        self.client.force_login(guest.user)

        response = self.client.get(reverse("spotguessr"), {"session": session.pk})

        self.assertIsNone(response.context["initial_session_id"])


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class KickedPlayerSocketTests(TransactionTestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _ = self.host.user, self.guest.user
        _befriend(self.host, self.guest)
        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest])
        join_session(self.session, self.guest)

    def _communicator(self, user) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(GameSessionConsumer.as_asgi(), f"/ws/spotguessr/session/{self.session.pk}/")
        comm.scope["url_route"] = {"kwargs": {"session_id": self.session.pk}}
        comm.scope["user"] = user
        return comm

    def test_the_kicked_players_open_socket_is_told_and_closed(self) -> None:
        _run(self._told_and_closed())

    async def _told_and_closed(self) -> None:
        comm = self._communicator(self.guest.user)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        with broadcasts_delivered_inline():
            await database_sync_to_async(kick_participant)(self.session, self.host, self.guest)
            relayed = await comm.receive_json_from(timeout=5)
            closed = await comm.receive_output(timeout=5)

        self.assertEqual((relayed["type"], relayed["reason"]), ("participant.left", "kicked"))
        self.assertEqual(closed["type"], "websocket.close")
        await comm.disconnect()

    def test_a_kicked_player_cannot_reconnect(self) -> None:
        kick_participant(self.session, self.host, self.guest)
        _run(self._cannot_reconnect())

    async def _cannot_reconnect(self) -> None:
        connected, close_code = await self._communicator(self.guest.user).connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)
