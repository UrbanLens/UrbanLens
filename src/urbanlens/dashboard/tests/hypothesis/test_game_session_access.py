"""A participant who left or was removed from a game session loses every route into it.

Covers the HTTP views, the join handshake, the WebSocket connect, a chat frame sent on a socket that outlived the
removal, and the shared chat service itself.
"""

from __future__ import annotations

from itertools import count
import json

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import TriviaSessionConsumer
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import (
    TriviaQuestion,
    TriviaQuestionSource,
    TriviaSessionChatMessage,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
)
from urbanlens.dashboard.services.core.session_access import NotAnActiveParticipantError
from urbanlens.dashboard.services.trivia import chat as trivia_chat
from urbanlens.dashboard.services.trivia.session import (
    NotInvitedError,
    TriviaConfig,
    begin_session,
    join_session,
    kick_participant,
    leave_session,
    start_multiplayer_session,
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


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _active_game(total_rounds: int = 2):
    host = _make_profile()
    guest = _make_profile()
    _befriend(host, guest)
    for _ in range(total_rounds):
        location = _make_location()
        baker.make(Pin, profile=host, location=location)
        baker.make(Pin, profile=guest, location=location)
        baker.make(TriviaQuestion, location=location, source=TriviaQuestionSource.DETERMINISTIC, answer="1937")
    session = start_multiplayer_session(host, TriviaConfig(), [guest], total_rounds=total_rounds)
    join_session(session, guest)
    assert begin_session(session, host) is not None
    return host, guest, session


class TriviaRemovedParticipantHttpTests(TestCase):
    def _get(self, profile: Profile, name: str, session_id: int):
        self.client.force_login(profile.user)
        return self.client.get(reverse(name, kwargs={"session_id": session_id}))

    def test_a_kicked_player_cannot_read_the_live_round(self) -> None:
        host, guest, session = _active_game()
        self.assertEqual(self._get(guest, "trivia.round", session.pk).status_code, 200)
        kick_participant(session, host, guest)

        response = self._get(guest, "trivia.round", session.pk)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"prompt", response.content)

    def test_a_player_who_left_cannot_read_chat_lobby_or_summary(self) -> None:
        host, guest, session = _active_game()
        trivia_chat.send_chat_message(session, host, "only for the table")
        leave_session(session, guest)

        for name in ("trivia.chat_history", "trivia.lobby", "trivia.summary"):
            with self.subTest(name=name):
                self.assertEqual(self._get(guest, name, session.pk).status_code, 404)

    def test_the_remaining_players_keep_access(self) -> None:
        host, guest, session = _active_game()
        kick_participant(session, host, guest)

        for name in ("trivia.round", "trivia.chat_history", "trivia.lobby", "trivia.summary"):
            with self.subTest(name=name):
                self.assertEqual(self._get(host, name, session.pk).status_code, 200)

    def test_an_invitee_who_has_not_joined_can_still_see_the_lobby(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, TriviaConfig(), [guest])

        self.assertEqual(self._get(guest, "trivia.lobby", session.pk).status_code, 200)

    def test_the_deep_link_does_not_reopen_a_session_the_player_left(self) -> None:
        host, guest, session = _active_game()
        leave_session(session, guest)
        self.client.force_login(guest.user)

        response = self.client.get(reverse("trivia"), {"session": session.pk})

        self.assertIsNone(response.context["initial_session_id"])


class TriviaRejoinTests(TestCase):
    def _lobby(self):
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        session = start_multiplayer_session(host, TriviaConfig(), [guest])
        join_session(session, guest)
        return host, guest, session

    def _status(self, session, profile) -> str:
        return TriviaSessionParticipant.objects.get(session=session, profile=profile).status

    def test_a_player_kicked_in_the_lobby_cannot_rejoin_over_http(self) -> None:
        host, guest, session = self._lobby()
        kick_participant(session, host, guest)
        self.client.force_login(guest.user)

        response = self.client.post(reverse("trivia.join", kwargs={"session_id": session.pk}))

        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(self._status(session, guest), TriviaSessionParticipantStatus.LEFT)

    def test_join_session_refuses_a_departed_profile(self) -> None:
        host, guest, session = self._lobby()
        leave_session(session, guest)

        with pytest.raises(NotInvitedError):
            join_session(session, guest)
        self.assertEqual(self._status(session, guest), TriviaSessionParticipantStatus.LEFT)

    def test_a_fresh_invite_lets_a_departed_profile_back_in(self) -> None:
        from urbanlens.dashboard.services.trivia.session import invite_to_session

        host, guest, session = self._lobby()
        kick_participant(session, host, guest)
        invite_to_session(session, host, guest)

        join_session(session, guest)

        self.assertEqual(self._status(session, guest), TriviaSessionParticipantStatus.JOINED)


class SessionChatServiceTests(TestCase):
    def test_a_departed_profile_cannot_post_chat_through_the_service(self) -> None:
        host, guest, session = _active_game()
        leave_session(session, guest)

        with pytest.raises(NotAnActiveParticipantError):
            trivia_chat.send_chat_message(session, guest, "still here?")
        self.assertFalse(TriviaSessionChatMessage.objects.filter(session=session, profile=guest).exists())

    def test_an_outsider_cannot_post_chat_through_the_service(self) -> None:
        host, guest, session = _active_game()
        outsider = _make_profile()

        with pytest.raises(NotAnActiveParticipantError):
            trivia_chat.send_chat_message(session, outsider, "hello")


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class TriviaRemovedParticipantSocketTests(TransactionTestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        _ = self.host.user, self.guest.user
        _befriend(self.host, self.guest)
        self.session = start_multiplayer_session(self.host, TriviaConfig(), [self.guest])
        join_session(self.session, self.guest)

    def _communicator(self, user) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(TriviaSessionConsumer.as_asgi(), f"/ws/trivia/session/{self.session.pk}/")
        comm.scope["url_route"] = {"kwargs": {"session_id": self.session.pk}}
        comm.scope["user"] = user
        return comm

    def test_a_kicked_player_cannot_reconnect(self) -> None:
        kick_participant(self.session, self.host, self.guest)
        _run(self._cannot_reconnect())

    async def _cannot_reconnect(self) -> None:
        comm = self._communicator(self.guest.user)
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_a_socket_that_outlived_the_removal_cannot_post_chat(self) -> None:
        _run(self._outlived_socket_cannot_post())
        self.assertFalse(TriviaSessionChatMessage.objects.filter(session=self.session, profile=self.guest).exists())

    async def _outlived_socket_cannot_post(self) -> None:
        comm = self._communicator(self.guest.user)
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        # The removal lands without the participant.left broadcast reaching this socket (a lost frame, another
        # worker), so only a check at receive time can stop the send.
        await database_sync_to_async(
            TriviaSessionParticipant.objects.filter(session=self.session, profile=self.guest).update,
        )(status=TriviaSessionParticipantStatus.LEFT)

        await comm.send_to(text_data=json.dumps({"body": "you can't see me"}))
        reply = await comm.receive_output(timeout=5)
        await comm.disconnect()

        self.assertEqual(reply["type"], "websocket.close")
