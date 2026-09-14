"""Volume and size bounds on inbound WebSocket frames."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from unittest import mock

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from model_bakery import baker
import yaml

from urbanlens.dashboard.checks import websocket_frame_cap_conflict
from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer
from urbanlens.dashboard.models.safety.model import SafetyCheckinMessage

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _run(coro):
    """Run *coro* through async_to_sync, matching the other consumer tests.

    ``database_sync_to_async``'s thread-sensitive mode needs the CurrentThreadExecutor that only async_to_sync's
    sync->async->sync bridge sets up; a coroutine driven by ``asyncio.run()`` hangs on the first database access
    instead of completing."""

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class WebSocketVolumeSettingsTests(SimpleTestCase):
    """The settings the other tests override have to exist in production.

    ``override_settings`` invents a name it does not find, so a budget test reading a misspelled setting passes
    green against code that reads nothing."""

    def test_the_frame_volume_settings_exist(self):
        for name in (
            "UL_WEBSOCKET_MAX_FRAME_CHARS",
            "UL_WEBSOCKET_FRAMES_PER_MINUTE",
            "UL_WEBSOCKET_FANOUT_FRAMES_PER_MINUTE",
            "UL_MESSAGES_PER_MINUTE",
            "UL_WEBSOCKET_MAX_MESSAGE_BYTES",
        ):
            with self.subTest(setting=name):
                self.assertTrue(hasattr(settings, name), f"{name} is not defined in settings")


class DaphneFrameSizeWiringTests(SimpleTestCase):
    """Daphne is actually started with a cap at or above the application's.

    Autobahn refuses an oversized frame at the protocol layer, with nothing sent and nothing logged, so a
    transport cap below the application cap reaches a user as an unexplained disconnect."""

    def _derived_flags(self, chars: str | None) -> list[str]:
        env = {"PATH": os.environ["PATH"]}
        if chars is not None:
            env["UL_WEBSOCKET_MAX_FRAME_CHARS"] = chars
        return subprocess.run(  # noqa: S603
            ["bash", str(REPO_ROOT / "bin" / "websocket_frame_flags.sh")],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.split()

    def test_the_cap_daphne_gets_tracks_the_documented_setting(self):
        """The one knob has to reach daphne, whatever the operator set it to.

        Written into docker-compose.yml instead, the flag's value would be substituted by compose from the shell
        before any container starts, and could not track a cap Django derives at import time."""
        argv = self._derived_flags("100000")

        for flag in ("--websocket-max-message-size", "--websocket-max-frame-size"):
            with self.subTest(flag=flag):
                self.assertIn(flag, argv)
                self.assertEqual(int(argv[argv.index(flag) + 1]), 400_000)

    def test_the_shell_default_matches_the_python_default(self):
        """Unset, both sides have to land on the same number.

        They are derived twice - once in shell for daphne's argv, once in settings/base.py for the guard - so
        the defaults are the one place the two derivations can silently disagree."""
        argv = self._derived_flags(None)
        derived = int(argv[argv.index("--websocket-max-message-size") + 1])

        self.assertEqual(derived, settings.UL_WEBSOCKET_MAX_MESSAGE_BYTES)

    def test_the_entrypoint_resolves_the_helper_where_the_image_puts_it(self):
        """The path has to be absolute, and the entrypoint has to die without it.

        The image copies the entrypoint to ``/`` and the repository to ``/app``, so resolving the helper
        relative to ``dirname "$0"`` looks for it in the root directory."""
        entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text()

        self.assertIn("readarray -t ws_frame_flags < <(/app/bin/websocket_frame_flags.sh)", entrypoint)
        self.assertIn("refusing to start daphne without its frame caps", entrypoint)

    def test_the_helper_is_executable(self):
        """The entrypoint tests -x, so a lost mode bit stops app-ws dead."""
        self.assertTrue(os.access(REPO_ROOT / "bin" / "websocket_frame_flags.sh", os.X_OK))

    def test_compose_does_not_also_hardcode_the_cap(self):
        """Two places to write the number is how the two numbers drift apart."""
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        command = compose["services"]["app-ws"]["command"]
        self.assertNotIn("--websocket-max-message-size", command)

    def test_a_transport_cap_below_the_application_cap_is_reported(self):
        """The guard for a deployment that passes the flags by hand anyway."""
        with mock.patch.object(sys, "argv", ["daphne", "--websocket-max-message-size", "1024", "app:application"]):
            self.assertIn("1024", websocket_frame_cap_conflict() or "")

    def test_nothing_is_reported_when_the_caps_agree(self):
        argv = [
            "daphne",
            "--websocket-max-message-size",
            str(settings.UL_WEBSOCKET_MAX_MESSAGE_BYTES),
            "app:application",
        ]
        with mock.patch.object(sys, "argv", argv):
            self.assertIsNone(websocket_frame_cap_conflict())

    def test_nothing_is_reported_when_the_process_carries_no_such_flag(self):
        """Every management command and test run takes this path."""
        with mock.patch.object(sys, "argv", ["manage.py", "migrate"]):
            self.assertIsNone(websocket_frame_cap_conflict())


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

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
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

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_the_refusal_is_an_error_frame_and_not_a_close(self):
        _run(self._refusal_is_an_error_frame())

    async def _refusal_is_an_error_frame(self):
        """A throttled sender is told, on the socket, without being disconnected.

        Closing would put the client into a reconnect loop over a condition retrying cannot fix - the convention
        ``_ParticipantSessionConsumer`` already documents."""
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

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
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

    @override_settings(UL_WEBSOCKET_MAX_FRAME_CHARS=256, UL_MESSAGES_PER_MINUTE=0)
    def test_an_oversized_frame_is_refused_before_it_is_parsed(self):
        _run(self._oversized_frame_is_refused_before_parsing())

    async def _oversized_frame_is_refused_before_parsing(self):
        """The size check has to run ahead of ``json.loads``, not after it.

        The frame sent here is both oversized *and* unparseable."""
        comm = self._owner_communicator()
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data="{" + "a" * 400)

        frame = json.loads(await comm.receive_from())
        self.assertEqual(frame["type"], "error")
        self.assertEqual(await self._acount_saved(), 0)
        await comm.disconnect()

    @override_settings(UL_WEBSOCKET_FRAMES_PER_MINUTE=3, UL_MESSAGES_PER_MINUTE=0)
    def test_frames_that_write_nothing_are_still_budgeted(self):
        _run(self._non_writing_frames_are_budgeted())

    async def _non_writing_frames_are_budgeted(self):
        """A keep-alive costs the server real work, so it is charged too.

        Budgeting only the writing frames leaves an unbounded flood of frames that each parse, dispatch and
        return - and on the DM socket the equivalent frame fans out to the *recipient's* tabs."""
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
