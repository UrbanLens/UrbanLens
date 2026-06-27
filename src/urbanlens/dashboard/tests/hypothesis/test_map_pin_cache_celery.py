"""Tests for Celery-backed map-pin cache rebuild enqueueing."""

from __future__ import annotations

from unittest import mock

import redis

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.map_pins.cache import MapPinCache


class _Profile:
    pk = 42


class MapPinCacheCeleryTests(TestCase):
    """MapPinCache queues one Celery rebuild and clears the guard on enqueue failure."""

    def test_enqueue_rebuild_queues_task_when_guard_key_is_set(self) -> None:
        client = mock.Mock()
        client.set.return_value = True
        cache = MapPinCache(_Profile(), client=client)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=mock.Mock(id="task")) as enqueue:
            cache.enqueue_rebuild()

        client.set.assert_called_once_with(cache.rebuild_queued_key, "1", nx=True, ex=cache.LOCK_SECONDS)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], 42)
        client.delete.assert_not_called()

    def test_enqueue_rebuild_skips_when_rebuild_already_queued(self) -> None:
        client = mock.Mock()
        client.set.return_value = None
        cache = MapPinCache(_Profile(), client=client)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            cache.enqueue_rebuild()

        enqueue.assert_not_called()

    def test_enqueue_rebuild_clears_guard_when_enqueue_fails(self) -> None:
        client = mock.Mock()
        client.set.return_value = True
        cache = MapPinCache(_Profile(), client=client)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=None):
            cache.enqueue_rebuild()

        client.delete.assert_called_once_with(cache.rebuild_queued_key)

    def test_enqueue_rebuild_handles_redis_errors(self) -> None:
        client = mock.Mock()
        client.set.side_effect = redis.exceptions.RedisError("down")
        cache = MapPinCache(_Profile(), client=client)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            cache.enqueue_rebuild()

        enqueue.assert_not_called()
