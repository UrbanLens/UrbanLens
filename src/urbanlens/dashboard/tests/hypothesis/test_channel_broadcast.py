"""Tests for the shared channel-layer dispatch boundary.

Covers:
- services.core.channel_broadcast.send_group_message() - no-ops without a channel
  layer, otherwise enqueues tasks.broadcast_channel_group_message via Celery
  rather than calling async_to_sync inline (see that module's docstring for
  why: gunicorn's gevent worker class and asyncio event loops don't mix).
- tasks.broadcast_channel_group_message() - the actual async_to_sync(
  channel_layer.group_send) call, run on celery-worker instead of inline in a
  request; tolerates a missing layer and a delivery failure without raising.
"""

from __future__ import annotations

from unittest import mock
from unittest.mock import AsyncMock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core import channel_broadcast
from urbanlens.dashboard.tasks import broadcast_channel_group_message


class SendGroupMessageTests(SimpleTestCase):
    """send_group_message no-ops without a layer, else enqueues the broadcast task."""

    def test_no_channel_layer_does_not_enqueue(self) -> None:
        with (
            mock.patch.object(channel_broadcast, "get_channel_layer", return_value=None),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            channel_broadcast.send_group_message("some-group", {"type": "x"})

        enqueue.assert_not_called()

    def test_channel_layer_present_enqueues_the_broadcast_task(self) -> None:
        with (
            mock.patch.object(channel_broadcast, "get_channel_layer", return_value=mock.Mock()),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            channel_broadcast.send_group_message("some-group", {"type": "x", "payload": 1})

        enqueue.assert_called_once_with(broadcast_channel_group_message, "some-group", {"type": "x", "payload": 1})


class BroadcastChannelGroupMessageTaskTests(SimpleTestCase):
    """The Celery task performs the real async_to_sync(group_send) call, tolerating failure."""

    def test_no_channel_layer_is_a_no_op(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.get_channel_layer", return_value=None):
            broadcast_channel_group_message("some-group", {"type": "x"})

    def test_delivers_to_the_layer(self) -> None:
        layer = mock.Mock()
        layer.group_send = AsyncMock()
        with mock.patch("urbanlens.dashboard.tasks.get_channel_layer", return_value=layer):
            broadcast_channel_group_message("some-group", {"type": "x", "payload": 1})

        layer.group_send.assert_awaited_once_with("some-group", {"type": "x", "payload": 1})

    def test_delivery_failure_is_logged_not_raised(self) -> None:
        layer = mock.Mock()
        layer.group_send = AsyncMock(side_effect=RuntimeError("valkey down"))
        with mock.patch("urbanlens.dashboard.tasks.get_channel_layer", return_value=layer):
            broadcast_channel_group_message("some-group", {"type": "x"})  # must not raise
