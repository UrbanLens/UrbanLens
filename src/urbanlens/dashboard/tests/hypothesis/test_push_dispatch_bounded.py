"""No push-dispatch task outlives its time limit, whatever the device limit is set to.

Delivery used to POST to every device in turn, 10 s each at worst, in one task; with the limit raised
to its 1,000 maximum, or to 0 (unlimited), that is far past the interactive queue's 150 s hard limit.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.push_device import PushTransport
from urbanlens.dashboard.services.core.task_limits import queue_defaults
from urbanlens.dashboard.services.notifications import push
from urbanlens.dashboard.services.sandbox.queues import Queue
from urbanlens.dashboard.tasks import dispatch_push_to_devices
from urbanlens.dashboard.tests.hypothesis.test_push_devices import _fake_resolution

_POST = "urbanlens.dashboard.services.notifications.push.requests.post"
_ENQUEUE = "urbanlens.dashboard.services.notifications.push.safely_enqueue_task"


class PushBudgetTests(SimpleTestCase):
    def test_one_batch_fits_the_interactive_soft_limit_even_if_every_endpoint_hangs(self) -> None:
        rounds = -(-push.PUSH_BATCH_SIZE // push.PUSH_CONCURRENCY)
        self.assertLess(rounds * push.DISPATCH_DEADLINE_SECONDS, queue_defaults()[Queue.INTERACTIVE].soft)


class PushBatchingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))
        resolution = _fake_resolution("8.8.8.8")
        resolution.start()
        self.addCleanup(resolution.stop)
        self.devices = [
            push.register_device(
                self.profile, transport=PushTransport.UNIFIEDPUSH, address=f"https://ntfy.example.com/up{n}"
            )
            for n in range(5)
        ]

    def test_devices_past_one_batch_go_to_further_tasks(self) -> None:
        with (
            mock.patch.object(push, "PUSH_BATCH_SIZE", 2),
            mock.patch(_POST, return_value=mock.Mock(status_code=200, is_redirect=False)) as post,
            mock.patch(_ENQUEUE) as enqueue,
        ):
            delivered = push.send_push_to_profile(self.profile.pk, {"title": "Hi"})

        self.assertEqual(delivered, 2)
        self.assertEqual(post.call_count, 2)
        handed_off = [call.args[1] for call in enqueue.call_args_list]
        self.assertTrue(all(call.args[0] is dispatch_push_to_devices for call in enqueue.call_args_list))
        self.assertEqual(sorted(pk for batch in handed_off for pk in batch), sorted(d.pk for d in self.devices[2:]))
        self.assertTrue(all(len(batch) <= 2 for batch in handed_off))

    def test_a_batch_delivers_concurrently(self) -> None:
        both_in_flight = threading.Barrier(2, timeout=5)

        def post(*_args, **_kwargs):
            both_in_flight.wait()
            return mock.Mock(status_code=200, is_redirect=False)

        with mock.patch(_POST, side_effect=post):
            delivered = push.send_push_to_devices([d.pk for d in self.devices[:2]], {"title": "Hi"})

        self.assertEqual(delivered, 2)

    def test_the_handed_off_task_delivers_its_batch(self) -> None:
        with mock.patch(_POST, return_value=mock.Mock(status_code=200, is_redirect=False)) as post:
            delivered = dispatch_push_to_devices([d.pk for d in self.devices[:3]], {"title": "Hi"})

        self.assertEqual(delivered, 3)
        self.assertEqual(post.call_count, 3)
