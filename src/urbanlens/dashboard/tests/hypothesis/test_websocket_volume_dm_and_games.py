"""Volume bounds on the direct-message and game-session sockets.

The safety check-in socket was converted first, to prove the mixin's shape
against one real consumer (P31). These are the other two families, and each has
a hazard the safety socket does not:

- **The DM socket fans out to somebody else's group.** A ``typing`` frame writes
  no row, but it broadcasts into the *recipient's* group - so leaving it
  unbudgeted, as the "only writes cost anything" reading would, leaves the
  cheapest amplifier on the socket unmetered.
- **The DM socket had no ``isinstance(data, dict)`` guard at all.** A frame of
  ``[]`` parses as valid JSON and then raises ``AttributeError`` on ``.get``,
  which kills the connection. The game socket has the guard; this one never did.
- **The game socket is one class serving three games**, so a bound added here
  has to be exercised through a real subclass rather than the private base.

Order of assertions matters in the group-chat case: an empty frame must not
spend the write budget, or a client with a bug in its composer would throttle
its user out of a conversation they never typed in.

``TransactionTestCase`` for the same reason as every other consumer test here,
and the cache is cleared per test because that class carries no cache isolation
and the budgets are cache counters.
"""

from __future__ import annotations

import asyncio
from itertools import count
import json
import time

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.dashboard.consumers import DirectMessageConsumer, TriviaSessionConsumer
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import TriviaSessionChatMessage
from urbanlens.dashboard.services.trivia.session import TriviaConfig, start_multiplayer_session

_coordinate_counter = count()


def _run(coro):
    """Run *coro* via async_to_sync so database_sync_to_async's thread bridge is pumped."""

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


def _make_profile(*, alpha: bool = False) -> Profile:
    user = baker.make("auth.User")
    if alpha:
        grant_alpha_features(user)
    return Profile.objects.get(user=user)


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


async def _drain(comm: WebsocketCommunicator) -> list[dict]:
    """Everything the socket has to say, so a count can be taken of it."""
    frames = []
    while not await comm.receive_nothing(timeout=0.3):
        frames.append(json.loads(await comm.receive_from()))
    return frames


async def _settle(count_rows) -> int:
    """Wait for the consumer to finish the frames already sent to it.

    ``send_to`` only enqueues, and neither of these sockets answers an accepted
    frame on the connection that sent it - the direct-message broadcast goes out
    through Celery, and the game broadcast reaches the *group*. So draining what
    comes back is not a synchronisation point here: it returns as soon as
    nothing has arrived, which is immediately, and the assertion then counts
    rows the consumer has not written yet. A flood test written that way reads
    "1" and looks like a working throttle.

    Polls until the count holds still across two reads, which is the only
    signal available when the socket says nothing.

    Args:
        count_rows: Async callable returning the row count to watch.

    Returns:
        The settled count.
    """
    deadline = time.monotonic() + 10.0
    previous = -1
    while time.monotonic() < deadline:
        current = await count_rows()
        if current == previous:
            return current
        previous = current
        await asyncio.sleep(0.4)
    return await count_rows()


class DirectMessageVolumeTests(TransactionTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        self.sender = _make_profile()
        self.recipient = _make_profile()
        _ = self.sender.user, self.recipient.user
        _befriend(self.sender, self.recipient)

    def _communicator(self, profile: Profile) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(DirectMessageConsumer.as_asgi(), "/ws/messages/")
        comm.scope["url_route"] = {"kwargs": {}}
        comm.scope["user"] = profile.user
        return comm

    @database_sync_to_async
    def _acount_messages(self) -> int:
        return DirectMessage.objects.filter(sender=self.sender).count()

    def test_a_non_object_frame_does_not_kill_the_connection(self) -> None:
        """``[]`` is valid JSON and was an AttributeError on ``.get`` one line later.

        The socket has no other guard against it, so before this the cheapest
        possible frame closed somebody's messaging connection.
        """
        _run(self._non_object_frame_survives())

    async def _non_object_frame_survives(self) -> None:
        comm = self._communicator(self.sender)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=json.dumps([]))
        await comm.send_to(text_data=json.dumps("5"))
        await comm.send_to(text_data=json.dumps(7))
        await _drain(comm)

        # Still alive: a real frame after the bad ones still gets through.
        await comm.send_to(text_data=json.dumps({"recipient": self.recipient.slug, "body": "still here"}))
        await _drain(comm)

        self.assertEqual(await _settle(self._acount_messages), 1)
        await comm.disconnect()

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_a_flood_of_direct_messages_stops_at_the_budget(self) -> None:
        _run(self._flood_stops_at_the_budget())

    async def _flood_stops_at_the_budget(self) -> None:
        comm = self._communicator(self.sender)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for index in range(9):
            await comm.send_to(text_data=json.dumps({"recipient": self.recipient.slug, "body": f"message {index}"}))
        await _drain(comm)

        self.assertEqual(await _settle(self._acount_messages), 3, "the write budget did not stop the flood")
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_FANOUT_FRAMES_PER_MINUTE=2)
    def test_typing_indicators_are_budgeted(self) -> None:
        """The one frame on this socket that writes nothing and still fans out.

        It broadcasts into the *recipient's* group, so an unbudgeted ``typing``
        is the cheapest amplifier here: no row, no validation, one channel-layer
        send per frame to somebody else's open tabs.
        """
        _run(self._typing_is_budgeted())

    async def _typing_is_budgeted(self) -> None:
        comm = self._communicator(self.sender)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for _ in range(6):
            await comm.send_to(text_data=json.dumps({"type": "typing", "recipient": self.recipient.slug}))
        frames = await _drain(comm)

        self.assertTrue(
            any(frame.get("type") == "error" for frame in frames),
            "a typing flood was never refused, so the frame is unbudgeted",
        )
        await comm.disconnect()

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_an_empty_frame_does_not_spend_the_write_budget(self) -> None:
        """A frame with nothing to send is dropped before it is charged.

        Otherwise a client bug that emitted blank frames would throttle its user
        out of a conversation they never typed in.
        """
        _run(self._empty_frames_are_free())

    async def _empty_frames_are_free(self) -> None:
        comm = self._communicator(self.sender)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for _ in range(5):
            await comm.send_to(text_data=json.dumps({"recipient": self.recipient.slug, "body": "   "}))
        await _drain(comm)
        for index in range(2):
            await comm.send_to(text_data=json.dumps({"recipient": self.recipient.slug, "body": f"real {index}"}))
        await _drain(comm)

        self.assertEqual(await _settle(self._acount_messages), 2, "blank frames spent the write budget")
        await comm.disconnect()

    def test_an_oversized_frame_is_refused_before_it_is_parsed(self) -> None:
        _run(self._oversized_frame_refused())

    async def _oversized_frame_refused(self) -> None:
        """The body is 500 characters: well over the 200-character frame cap, and
        well under ``MAX_DIRECT_MESSAGE_LENGTH`` (1,000).

        Sizing it above the *body* limit instead would pass against unbounded
        code, because the service's own length check already answers an
        over-long body with an error frame and saves nothing - so the test would
        assert the frame cap and measure a validator.
        """
        comm = self._communicator(self.sender)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        with override_settings(UL_WEBSOCKET_MAX_FRAME_CHARS=200):
            await comm.send_to(text_data=json.dumps({"recipient": self.recipient.slug, "body": "x" * 500}))
            frames = await _drain(comm)

        self.assertEqual(await _settle(self._acount_messages), 0, "an oversized frame was accepted and saved")
        self.assertTrue(any(frame.get("type") == "error" for frame in frames))
        await comm.disconnect()


class TriviaSessionVolumeTests(TransactionTestCase):
    """Exercised through a real subclass: ``_ParticipantSessionConsumer`` serves three games."""

    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        self.host = _make_profile(alpha=True)
        self.guest = _make_profile(alpha=True)
        _ = self.host.user, self.guest.user
        _befriend(self.host, self.guest)
        baker.make(Location, latitude="42.650000", longitude="-73.760000")
        self.session = start_multiplayer_session(self.host, TriviaConfig(), [self.guest])

    def _communicator(self, profile: Profile) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(TriviaSessionConsumer.as_asgi(), f"/ws/trivia/session/{self.session.pk}/")
        comm.scope["url_route"] = {"kwargs": {"session_id": self.session.pk}}
        comm.scope["user"] = profile.user
        return comm

    @database_sync_to_async
    def _acount_chat(self) -> int:
        return TriviaSessionChatMessage.objects.filter(session=self.session).count()

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_a_chat_flood_stops_at_the_budget(self) -> None:
        _run(self._flood_stops_at_the_budget())

    async def _flood_stops_at_the_budget(self) -> None:
        comm = self._communicator(self.host)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for index in range(9):
            await comm.send_to(text_data=json.dumps({"body": f"message {index}"}))
        await _drain(comm)

        self.assertEqual(await _settle(self._acount_chat), 3, "the write budget did not stop the flood")
        await comm.disconnect()

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_the_refusal_is_an_error_frame_and_not_a_close(self) -> None:
        """Closing would put the client into a reconnect loop over a condition
        retrying cannot fix - the convention this consumer's own scope refusal
        already follows."""
        _run(self._refusal_is_an_error_frame())

    async def _refusal_is_an_error_frame(self) -> None:
        comm = self._communicator(self.host)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for index in range(6):
            await comm.send_to(text_data=json.dumps({"body": f"message {index}"}))
        frames = await _drain(comm)

        self.assertTrue(any(frame.get("type") == "error" for frame in frames), "no error frame was sent")
        self.assertTrue(await comm.receive_nothing(timeout=0.2))
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_FRAMES_PER_MINUTE=2)
    def test_a_ping_is_charged_to_the_frame_budget(self) -> None:
        """Keep-alives are traffic, and traffic is what the frame tier bounds.

        Exempting them would leave the socket with an unmetered frame anyone can
        send: it parses, dispatches, and is free.
        """
        _run(self._pings_are_charged())

    async def _pings_are_charged(self) -> None:
        comm = self._communicator(self.host)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for _ in range(4):
            await comm.send_to(text_data=json.dumps({"type": "ping"}))
        frames = await _drain(comm)

        self.assertTrue(
            any(frame.get("type") == "error" for frame in frames), "pings are not charged to the frame budget"
        )
        await comm.disconnect()

    @override_settings(UL_MESSAGES_PER_MINUTE=1)
    def test_a_ping_does_not_spend_the_message_budget(self) -> None:
        """The client sends one every 45 seconds, because Cloudflare closes an idle
        tunnelled socket at about 100. Charging those against the write budget
        would throttle a user for sitting in a lobby.

        The frame budget is left at its production value here, so the pings are
        charged where they belong and the message that follows still arrives.
        """
        _run(self._pings_are_not_writes())

    async def _pings_are_not_writes(self) -> None:
        comm = self._communicator(self.host)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for _ in range(5):
            await comm.send_to(text_data=json.dumps({"type": "ping"}))
        await comm.send_to(text_data=json.dumps({"body": "after the pings"}))
        await _drain(comm)

        self.assertEqual(await _settle(self._acount_chat), 1, "pings spent the write budget")
        await comm.disconnect()

    def test_an_oversized_frame_is_refused_before_it_is_parsed(self) -> None:
        _run(self._oversized_frame_refused())

    async def _oversized_frame_refused(self) -> None:
        comm = self._communicator(self.host)
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        with override_settings(UL_WEBSOCKET_MAX_FRAME_CHARS=200):
            await comm.send_to(text_data=json.dumps({"body": "x" * 5000}))
            frames = await _drain(comm)

        self.assertTrue(any(frame.get("type") == "error" for frame in frames))
        self.assertEqual(await _settle(self._acount_chat), 0)
        await comm.disconnect()
