"""A group message must not become one Celery task per member.

`send_group_message` routes every channel-layer send through a Celery task,
deliberately: calling `async_to_sync` inline under gevent can poison an
unrelated in-flight request (see services/core/channel_broadcast). But the group
chat calls it once per member, so one message to a fifty-person group enqueues
fifty tasks, and each one builds its own event loop and its own Valkey
connection to deliver a single frame (N21 H41).

The per-member *payload* still has to be built per member - a message carries
the sender's name, resolved through each viewer's own visibility, and that is
the accepted price of never leaking a masked name over the live channel. What
does not have to be per member is the delivery.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core import channel_broadcast

_ENQUEUE = "urbanlens.dashboard.services.core.channel_broadcast.safely_enqueue_task"


class OneMessageIsOneTaskTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _deliveries(self, count: int) -> list[tuple[str, dict]]:
        return [(f"dm_{index}", {"type": "dm.message", "message": {"body": "hi"}}) for index in range(count)]

    def test_a_batch_is_enqueued_once_however_many_recipients(self) -> None:
        with (
            mock.patch(_ENQUEUE) as enqueue,
            mock.patch.object(channel_broadcast, "get_channel_layer", return_value=object()),
        ):
            channel_broadcast.send_group_messages(self._deliveries(2))
            small = enqueue.call_count
            enqueue.reset_mock()
            channel_broadcast.send_group_messages(self._deliveries(50))
            large = enqueue.call_count

        self.assertEqual(small, 1)
        self.assertEqual(large, 1, f"fifty recipients enqueued {large} tasks")

    def test_every_recipient_is_still_in_the_batch(self) -> None:
        """The half that stops the test above passing against a send that dropped everyone."""
        deliveries = self._deliveries(5)

        with (
            mock.patch(_ENQUEUE) as enqueue,
            mock.patch.object(channel_broadcast, "get_channel_layer", return_value=object()),
        ):
            channel_broadcast.send_group_messages(deliveries)

        sent = enqueue.call_args.args[1]
        self.assertEqual([group for group, _ in sent], [group for group, _ in deliveries])

    def test_no_channel_layer_means_no_task(self) -> None:
        with (
            mock.patch(_ENQUEUE) as enqueue,
            mock.patch.object(channel_broadcast, "get_channel_layer", return_value=None),
        ):
            channel_broadcast.send_group_messages(self._deliveries(3))

        enqueue.assert_not_called()

    def test_an_empty_batch_enqueues_nothing(self) -> None:
        with (
            mock.patch(_ENQUEUE) as enqueue,
            mock.patch.object(channel_broadcast, "get_channel_layer", return_value=object()),
        ):
            channel_broadcast.send_group_messages([])

        enqueue.assert_not_called()


class TheGroupChatUsesTheBatchTests(TestCase):
    """A batched helper nothing calls fixes nothing."""

    def test_the_group_message_broadcast_sends_one_batch(self) -> None:
        import inspect

        from urbanlens.dashboard.services.messaging import group_chats

        source = inspect.getsource(group_chats)

        self.assertIn("send_group_messages(", source, "the group chat still delivers one channel send at a time")
