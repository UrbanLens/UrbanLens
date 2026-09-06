"""Volume and size bounds on inbound WebSocket frames.

Uses TransactionTestCase for the same reason every other consumer test here
does: Channels reaches the database from a background thread via
``database_sync_to_async``.

``TransactionTestCase`` does *not* carry ``core.tests.testcase``'s cache
isolation, and the budgets under test are cache counters, so every test clears
the cache itself. Without that a counter outlives its test and fails the next
one - the order-dependent failure that isolation exists to prevent.
"""

from __future__ import annotations

import json
import pathlib

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from model_bakery import baker
import yaml

from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer
from urbanlens.dashboard.models.safety.model import SafetyCheckinMessage

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _run(coro):
    """Run *coro* through async_to_sync, matching the other consumer tests.

    ``database_sync_to_async``'s thread-sensitive mode needs the
    CurrentThreadExecutor that only async_to_sync's sync->async->sync bridge
    sets up; a coroutine driven by ``asyncio.run()`` hangs on the first
    database access instead of completing.
    """

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class WebSocketVolumeSettingsTests(SimpleTestCase):
    """The settings the other tests override have to exist in production.

    ``override_settings`` invents a name it does not find, so a budget test
    reading a misspelled setting passes green against code that reads nothing.
    These assertions are what make the rest of this file mean anything.
    """

    def test_the_frame_volume_settings_exist(self):
        for name in (
            "UL_WEBSOCKET_MAX_FRAME_CHARS",
            "UL_WEBSOCKET_FRAMES_PER_MINUTE",
            "UL_WEBSOCKET_MESSAGES_PER_MINUTE",
            "UL_WEBSOCKET_MAX_MESSAGE_BYTES",
        ):
            with self.subTest(setting=name):
                self.assertTrue(hasattr(settings, name), f"{name} is not defined in settings")

    def test_the_daphne_byte_cap_is_looser_than_the_app_character_cap(self):
        """Daphne must never kill a socket over a frame the app would accept.

        Autobahn refuses an oversized frame at the protocol layer, with no
        error frame and no explanation - the user sees a random disconnect. So
        the transport bound has to sit above the application bound, at the
        worst case of four UTF-8 bytes per character.
        """
        self.assertGreaterEqual(
            settings.UL_WEBSOCKET_MAX_MESSAGE_BYTES,
            settings.UL_WEBSOCKET_MAX_FRAME_CHARS * 4,
        )


class DaphneFrameSizeWiringTests(SimpleTestCase):
    """The compose command actually passes the caps daphne needs."""

    def test_daphne_is_given_the_websocket_size_flags(self):
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        command = compose["services"]["app-ws"]["command"]
        # Hyphens. Daphne spells these two with hyphens while its neighbours
        # (--websocket_timeout) use underscores, and an unrecognised flag exits
        # daphne at startup with no other symptom.
        for flag in ("--websocket-max-message-size", "--websocket-max-frame-size"):
            with self.subTest(flag=flag):
                self.assertIn(flag, command)


@override_settings(UL_WEBSOCKET_FRAMES_PER_MINUTE=0)
class SafetyChatVolumeLimitTests(TransactionTestCase):
    """Inbound frames on the safety check-in chat socket are bounded.

    The frame budget is disabled for the class so each test exercises exactly
    the one bound it names; the frame-budget test re-enables it.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner_user = baker.make("auth.User")
        self.owner_profile = self.owner_user.profile
        self.checkin = baker.make("dashboard.SafetyCheckin", profile=self.owner_profile)

    def _owner_communicator(self) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(
            SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/checkin/{self.checkin.uuid}/chat/"
        )
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = self.owner_user
        return comm

    def _saved_bodies(self) -> list[str]:
        return list(SafetyCheckinMessage.objects.filter(checkin=self.checkin).values_list("body", flat=True))

    @override_settings(UL_WEBSOCKET_MESSAGES_PER_MINUTE=3)
    def test_a_flood_of_chat_frames_stops_at_the_budget(self):
        _run(self._flood_stops_at_the_budget())

    async def _flood_stops_at_the_budget(self):
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for index in range(8):
            await comm.send_to(text_data=json.dumps({"body": f"message {index}"}))
        # Drain rather than assert on the echoes: what is under test is how many
        # rows the flood produced, not what came back.
        while not await comm.receive_nothing(timeout=0.3):
            await comm.receive_from()

        saved = await self._acount_saved()
        self.assertEqual(saved, 3, "the write budget did not stop the flood")
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_MESSAGES_PER_MINUTE=2)
    def test_the_refusal_is_an_error_frame_and_not_a_close(self):
        _run(self._refusal_is_an_error_frame())

    async def _refusal_is_an_error_frame(self):
        """A throttled sender is told, on the socket, without being disconnected.

        Closing would put the client into a reconnect loop over a condition
        retrying cannot fix - the convention ``_ParticipantSessionConsumer``
        already documents. Asserting on the *error frame* rather than on "a
        frame came back" is what makes this fail today: an accepted message
        already echoes a ``websocket.send``, so a type-only assertion would
        pass against unthrottled code.
        """
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for index in range(5):
            await comm.send_to(text_data=json.dumps({"body": f"message {index}"}))

        details: list[str] = []
        while not await comm.receive_nothing(timeout=0.3):
            frame = json.loads(await comm.receive_from())
            if frame.get("type") == "error":
                details.append(frame["detail"])

        self.assertTrue(details, "a throttled sender got no error frame")
        self.assertTrue(await comm.receive_nothing(timeout=0.2))
        # The socket is still usable - nothing closed it.
        self.assertFalse(comm.future.done())
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_MESSAGES_PER_MINUTE=2)
    def test_one_error_frame_per_window_not_one_per_refused_frame(self):
        _run(self._one_error_frame_per_window())

    async def _one_error_frame_per_window(self):
        """Answering every refused frame turns an inbound flood into an outbound one."""
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for index in range(30):
            await comm.send_to(text_data=json.dumps({"body": f"message {index}"}))

        errors = 0
        while not await comm.receive_nothing(timeout=0.3):
            if json.loads(await comm.receive_from()).get("type") == "error":
                errors += 1

        self.assertEqual(errors, 1, "the throttle answered a flood with a flood")
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_MAX_FRAME_CHARS=256, UL_WEBSOCKET_MESSAGES_PER_MINUTE=0)
    def test_an_oversized_frame_is_refused_before_it_is_parsed(self):
        _run(self._oversized_frame_is_refused_before_parsing())

    async def _oversized_frame_is_refused_before_parsing(self):
        """The size check has to run ahead of ``json.loads``, not after it.

        The frame sent here is both oversized *and* unparseable. Today the
        consumer parses first, the parse fails, and it returns silently - so an
        error frame can only come back from a check ordered before the parse,
        which is exactly the ordering under test.
        """
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data="{" + "a" * 400)

        frame = json.loads(await comm.receive_from())
        self.assertEqual(frame["type"], "error")
        self.assertEqual(await self._acount_saved(), 0)
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_FRAMES_PER_MINUTE=3, UL_WEBSOCKET_MESSAGES_PER_MINUTE=0)
    def test_frames_that_write_nothing_are_still_budgeted(self):
        _run(self._non_writing_frames_are_budgeted())

    async def _non_writing_frames_are_budgeted(self):
        """A keep-alive costs the server real work, so it is charged too.

        Budgeting only the writing frames leaves an unbounded flood of frames
        that each parse, dispatch and return - and on the DM socket the
        equivalent frame fans out to the *recipient's* tabs.
        """
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        for _ in range(6):
            await comm.send_to(text_data=json.dumps({"type": "ping"}))
        # A ping is answered by nothing at all today, so the only frame that can
        # come back is the throttle telling us it stopped counting them.
        frame = json.loads(await comm.receive_from())
        self.assertEqual(frame["type"], "error")
        await comm.disconnect()

    async def _acount_saved(self) -> int:
        from channels.db import database_sync_to_async

        return await database_sync_to_async(SafetyCheckinMessage.objects.filter(checkin=self.checkin).count)()
