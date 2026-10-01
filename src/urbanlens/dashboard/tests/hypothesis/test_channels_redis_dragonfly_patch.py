"""channels_redis's backup-queue cleanup script must declare its keys for Dragonfly to accept it."""

from __future__ import annotations

import logging
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from urbanlens.dashboard.services.core import channels_redis_dragonfly_patch as patch_module


class _FakeConnection:
    """Records the ``eval``/``bzpopmin``/``zadd`` calls a real redis connection would make."""

    def __init__(self, bzpopmin_result):
        self.eval_calls: list[tuple] = []
        self.zadd_calls: list[tuple] = []
        self._bzpopmin_result = bzpopmin_result

    async def eval(self, script, numkeys, *keys_and_args):
        self.eval_calls.append((script, numkeys, keys_and_args))

    async def bzpopmin(self, channel, timeout):  # noqa: ASYNC109 - matching the real redis client's signature, which this fakes
        return self._bzpopmin_result

    async def zadd(self, key, mapping):
        self.zadd_calls.append((key, mapping))


class PatchBackupQueueScriptTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Each test re-applies the patch against a clean, unpatched method so tests don't depend on
        # ordering; channels_redis.core is a real module, so this mutates shared state.
        from channels_redis import core

        self._unpatched = (
            core.RedisChannelLayer.__dict__.get("_brpop_with_clean") or core.RedisChannelLayer._brpop_with_clean
        )
        self.addCleanup(setattr, core.RedisChannelLayer, "_brpop_with_clean", self._unpatched)

    def test_the_replacement_script_declares_both_keys_instead_of_argv(self) -> None:
        from channels_redis import core

        patch_module.patch_backup_queue_script()

        connection = _FakeConnection(bzpopmin_result=None)
        layer = mock.Mock(spec=core.RedisChannelLayer)
        layer._backup_channel_name.return_value = "asgispecific.deadbeef!backup"
        layer.connection.return_value = connection

        async_to_sync(core.RedisChannelLayer._brpop_with_clean)(layer, 0, "asgispecific.deadbeef!", timeout=1)

        self.assertEqual(len(connection.eval_calls), 1)
        script, numkeys, keys_and_args = connection.eval_calls[0]
        self.assertEqual(numkeys, 2, "the script must declare both keys via KEYS, not pass them as trailing ARGV")
        self.assertEqual(keys_and_args, ("asgispecific.deadbeef!", "asgispecific.deadbeef!backup"))
        self.assertNotIn("ARGV", script, "the replacement script must not fall back to ARGV for its keys")
        self.assertIn("KEYS[1]", script)
        self.assertIn("KEYS[2]", script)

    def test_a_popped_member_is_still_added_to_the_backup_queue(self) -> None:
        """Anti-vacuity: the patch changes only how keys are declared, not the reliable-delivery behavior."""
        from channels_redis import core

        patch_module.patch_backup_queue_script()

        connection = _FakeConnection(bzpopmin_result=("asgispecific.deadbeef!", "message-id", 123.0))
        layer = mock.Mock(spec=core.RedisChannelLayer)
        layer._backup_channel_name.return_value = "asgispecific.deadbeef!backup"
        layer.connection.return_value = connection

        member = async_to_sync(core.RedisChannelLayer._brpop_with_clean)(layer, 0, "asgispecific.deadbeef!", timeout=1)

        self.assertEqual(member, "message-id")
        self.assertEqual(connection.zadd_calls, [("asgispecific.deadbeef!backup", {"message-id": 123.0})])

    def test_calling_it_twice_keeps_the_same_wrapper_installed(self) -> None:
        from channels_redis import core

        patch_module.patch_backup_queue_script()
        first = core.RedisChannelLayer.__dict__["_brpop_with_clean"]

        patch_module.patch_backup_queue_script()
        second = core.RedisChannelLayer.__dict__["_brpop_with_clean"]

        self.assertIs(first, second)

    def test_an_unverified_channels_redis_version_is_left_unpatched(self) -> None:
        import channels_redis
        from channels_redis import core

        # apps.py's own ready() hook already applied this patch to the real channels_redis for
        # whatever version this test process actually has installed, so the class attribute may
        # already carry the marker - install a deliberately unmarked stand-in so the version guard
        # (checked after the marker guard) is what this test actually exercises.
        async def unmarked_original(self, index, channel, timeout):  # noqa: ASYNC109 - matching the real method's signature
            raise AssertionError("should not be called")

        core.RedisChannelLayer._brpop_with_clean = unmarked_original

        with (
            mock.patch.object(channels_redis, "__version__", "99.0.0"),
            self.assertLogs(patch_module.logger, level=logging.WARNING),
        ):
            patch_module.patch_backup_queue_script()

        self.assertIs(core.RedisChannelLayer._brpop_with_clean, unmarked_original)
