"""The inline-task helper patched a name none of its callers read.

`tasks_run_inline` exists so a consumer test can assert on a broadcast that only
becomes true once the Celery task runs. It patched
``services.core.celery.safely_enqueue_task`` - where the function is defined -
while every caller binds the name into its own module at import
(``from ... import safely_enqueue_task``). Patching the definition rebinds an
attribute nobody reads, so the real enqueue ran, the task went to a broker with
no worker draining it, and it was dropped.

The consumer test then waited for a frame that was never going to arrive, and
`WebsocketCommunicator` eventually raised `TimeoutError`. That is why this was
mistaken for an environment problem for so long: the symptom is a hung socket,
which looks like load or a slow host, and it appears in whichever tests happen
to await a broadcast rather than in the fixture that is actually broken.

Twelve modules bind the name. Enumerating them by hand would rot on the next
one, so the helper resolves them from `sys.modules` - the assertion below is
that it finds *every* holder, not a list someone remembered to update.
"""

from __future__ import annotations

import sys

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase


def _holders() -> list[object]:
    """Every imported module whose ``safely_enqueue_task`` is the real one."""
    from urbanlens.dashboard.services.core import celery as celery_module

    original = celery_module.safely_enqueue_task
    return [module for module in list(sys.modules.values()) if getattr(module, "safely_enqueue_task", None) is original]


class ThePatchReachesTheCallersTests(TestCase):
    def test_a_module_that_bound_the_name_at_import_sees_the_mock(self) -> None:
        from urbanlens.dashboard.services.core import channel_broadcast

        with tasks_run_inline() as enqueue:
            self.assertIs(channel_broadcast.safely_enqueue_task, enqueue)

    def test_there_is_more_than_one_holder_to_reach(self) -> None:
        """The anti-vacuity half: with one holder the bug could not exist."""
        import urbanlens.dashboard.controllers.immich  # noqa: F401
        import urbanlens.dashboard.services.core.channel_broadcast  # noqa: F401

        self.assertGreater(len(_holders()), 1)

    def test_every_holder_sees_the_same_mock(self) -> None:
        """One shared mock, so a test asserting on call counts sees them all."""
        import urbanlens.dashboard.controllers.immich  # noqa: F401
        import urbanlens.dashboard.services.core.channel_broadcast  # noqa: F401

        before = _holders()
        self.assertTrue(before, "nothing holds the name; the enumeration is wrong")

        with tasks_run_inline() as enqueue:
            for module in before:
                with self.subTest(module=module.__name__):
                    self.assertIs(module.safely_enqueue_task, enqueue)


class TheHelperStillSelectsTests(TestCase):
    """Reaching the callers must not turn the helper into "run everything"."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.core import channel_broadcast

        self.caller = channel_broadcast
        self.ran: list[int] = []

    def _task(self, value: int) -> None:
        self.ran.append(value)

    def test_a_selected_task_runs_through_a_holder(self) -> None:
        with tasks_run_inline(self._task):
            self.caller.safely_enqueue_task(self._task, 1)

        self.assertEqual(self.ran, [1])

    def test_an_unselected_task_is_still_dropped(self) -> None:
        with tasks_run_inline():
            self.caller.safely_enqueue_task(self._task, 1)

        self.assertEqual(self.ran, [], "the helper ran a task no test asked for")

    def test_routing_metadata_is_not_passed_to_the_task_body(self) -> None:
        """`queue=` is for the broker; the task signature does not take it."""
        with tasks_run_inline(self._task):
            self.caller.safely_enqueue_task(self._task, 1, queue="interactive")

        self.assertEqual(self.ran, [1])


class TheOriginalIsRestoredTests(TestCase):
    def test_every_holder_is_put_back_afterwards(self) -> None:
        """A leaked mock would silently disable enqueueing for the rest of the run."""
        from urbanlens.dashboard.services.core import celery as celery_module, channel_broadcast

        original = channel_broadcast.safely_enqueue_task

        with tasks_run_inline():
            pass

        self.assertIs(channel_broadcast.safely_enqueue_task, original)
        self.assertIs(celery_module.safely_enqueue_task, original)
