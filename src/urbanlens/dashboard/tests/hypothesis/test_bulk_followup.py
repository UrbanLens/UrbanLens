"""One bulk action's per-row signals must not create one broker message per row (P109).

Importing 1,000 pins queued about 2,600 follow-on tasks - wiki creation, enrichment, category
suggestion, reputation scoring, one set per pin - onto the same FIFO ``bulk`` queue every other
account's bulk jobs share, at low concurrency. A safety-escalation task queued behind ~2,640 of
them, 220 minutes to clear. ``batching_follow_on_work`` bounds that: wrapped around a bulk action's
per-row loop, it buffers ids and flushes chunk-shaped batch tasks instead of one task per row, so
queue depth tracks ``rows / chunk_size`` rather than ``rows``. Outside that context,
``enqueue_follow_on`` enqueues exactly as ``safely_enqueue_task`` always has - a signal fired by
someone's own single save is unaffected.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.bulk_followup import (
    DEFAULT_CHUNK_SIZE,
    BulkFollowUpCollector,
    batching_follow_on_work,
    enqueue_follow_on,
)
from urbanlens.dashboard.services.sandbox.queues import Queue

ENQUEUE = "urbanlens.dashboard.services.core.bulk_followup.safely_enqueue_task"


def _task(name: str) -> mock.Mock:
    """A stand-in Celery task: enough like the real thing for ``.name`` to work as a dict key."""
    return mock.Mock(name=name, spec=["name", "apply_async"])


ITEM_TASK = _task("item_task")
BATCH_TASK = _task("batch_task")
OTHER_ITEM_TASK = _task("other_item_task")
OTHER_BATCH_TASK = _task("other_batch_task")


class EnqueueFollowOnOutsideAnyCollectorTests(SimpleTestCase):
    """No collector active: behaves exactly as a direct ``safely_enqueue_task`` call always has."""

    def test_enqueues_the_item_task_immediately(self) -> None:
        with mock.patch(ENQUEUE) as enqueue:
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 7, queue=Queue.BULK)

        enqueue.assert_called_once_with(ITEM_TASK, 7, queue=Queue.BULK)

    def test_never_touches_the_batch_task(self) -> None:
        with mock.patch(ENQUEUE) as enqueue:
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 7, queue=None)

        for call in enqueue.call_args_list:
            self.assertIsNot(call.args[0], BATCH_TASK)

    def test_a_none_queue_is_kept(self) -> None:
        with mock.patch(ENQUEUE) as enqueue:
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 7, queue=None)

        enqueue.assert_called_once_with(ITEM_TASK, 7, queue=None)


class BulkFollowUpCollectorTests(SimpleTestCase):
    """The buffering primitive ``batching_follow_on_work`` opens, tested directly."""

    def test_buffers_below_chunk_size_without_enqueuing(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=5)

        with mock.patch(ENQUEUE) as enqueue:
            for item_id in range(4):
                collector.add(BATCH_TASK, item_id, queue=Queue.BULK)

        enqueue.assert_not_called()

    def test_flushes_a_chunk_the_moment_it_fills(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=3)

        with mock.patch(ENQUEUE) as enqueue:
            collector.add(BATCH_TASK, 1, queue=Queue.BULK)
            collector.add(BATCH_TASK, 2, queue=Queue.BULK)
            enqueue.assert_not_called()
            collector.add(BATCH_TASK, 3, queue=Queue.BULK)

        enqueue.assert_called_once_with(BATCH_TASK, [1, 2, 3], queue=Queue.BULK)

    def test_a_flushed_chunk_does_not_repeat_in_the_next_one(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=2)

        with mock.patch(ENQUEUE) as enqueue:
            for item_id in range(5):
                collector.add(BATCH_TASK, item_id, queue=Queue.BULK)

        self.assertEqual([call.args[1] for call in enqueue.call_args_list], [[0, 1], [2, 3]])

    def test_flush_all_enqueues_a_partial_remainder(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=10)
        collector.add(BATCH_TASK, 1, queue=Queue.BULK)
        collector.add(BATCH_TASK, 2, queue=Queue.BULK)

        with mock.patch(ENQUEUE) as enqueue:
            collector.flush_all()

        enqueue.assert_called_once_with(BATCH_TASK, [1, 2], queue=Queue.BULK)

    def test_flush_all_on_an_empty_collector_enqueues_nothing(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=10)

        with mock.patch(ENQUEUE) as enqueue:
            collector.flush_all()

        enqueue.assert_not_called()

    def test_flush_all_is_idempotent(self) -> None:
        """A second flush must not re-send an already-flushed chunk."""
        collector = BulkFollowUpCollector(chunk_size=10)
        collector.add(BATCH_TASK, 1, queue=Queue.BULK)

        with mock.patch(ENQUEUE) as enqueue:
            collector.flush_all()
            collector.flush_all()

        enqueue.assert_called_once()

    def test_different_batch_tasks_are_chunked_independently(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=2)

        with mock.patch(ENQUEUE) as enqueue:
            collector.add(BATCH_TASK, 1, queue=Queue.BULK)
            collector.add(OTHER_BATCH_TASK, 100, queue=Queue.BULK)
            enqueue.assert_not_called()
            collector.add(BATCH_TASK, 2, queue=Queue.BULK)

        enqueue.assert_called_once_with(BATCH_TASK, [1, 2], queue=Queue.BULK)

    def test_each_batch_tasks_own_queue_is_kept_independently(self) -> None:
        collector = BulkFollowUpCollector(chunk_size=1)

        with mock.patch(ENQUEUE) as enqueue:
            collector.add(BATCH_TASK, 1, queue=Queue.BULK)
            collector.add(OTHER_BATCH_TASK, 2, queue=None)

        enqueue.assert_has_calls(
            [mock.call(BATCH_TASK, [1], queue=Queue.BULK), mock.call(OTHER_BATCH_TASK, [2], queue=None)],
            any_order=True,
        )


class BatchingFollowOnWorkContextTests(SimpleTestCase):
    """``batching_follow_on_work`` is what makes ``enqueue_follow_on`` buffer instead of enqueuing."""

    def test_outside_the_context_enqueue_follow_on_is_immediate(self) -> None:
        with mock.patch(ENQUEUE) as enqueue:
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 1, queue=Queue.BULK)

        enqueue.assert_called_once_with(ITEM_TASK, 1, queue=Queue.BULK)

    def test_inside_the_context_calls_below_chunk_size_are_buffered_not_enqueued(self) -> None:
        with mock.patch(ENQUEUE) as enqueue, batching_follow_on_work(chunk_size=5):
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 1, queue=Queue.BULK)
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 2, queue=Queue.BULK)

            enqueue.assert_not_called()

    def test_the_context_flushes_whatever_is_left_on_exit(self) -> None:
        with mock.patch(ENQUEUE) as enqueue, batching_follow_on_work(chunk_size=5):
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 1, queue=Queue.BULK)
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 2, queue=Queue.BULK)
            enqueue.assert_not_called()

        enqueue.assert_called_once_with(BATCH_TASK, [1, 2], queue=Queue.BULK)

    def test_a_large_batch_is_bounded_to_chunk_sized_pieces_not_one_task_each(self) -> None:
        """The headline claim: N rows produce ceil(N / chunk_size) tasks, not N."""
        with mock.patch(ENQUEUE) as enqueue, batching_follow_on_work(chunk_size=50):
            for item_id in range(120):
                enqueue_follow_on(ITEM_TASK, BATCH_TASK, item_id, queue=Queue.BULK)

        self.assertEqual(
            enqueue.call_count, 3, "120 rows at a chunk size of 50 should flush as 50 + 50 + 20, not one task per row"
        )
        flushed_ids = [item_id for call in enqueue.call_args_list for item_id in call.args[1]]
        self.assertEqual(
            sorted(flushed_ids),
            list(range(120)),
            "every row's id must reach a batch task exactly once - none lost, none duplicated",
        )

    def test_leaving_the_context_after_an_exception_still_flushes(self) -> None:
        """A failed import must not silently drop the follow-on work for the rows it did finish."""
        with mock.patch(ENQUEUE) as enqueue, self.assertRaises(RuntimeError), batching_follow_on_work(chunk_size=5):
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 1, queue=Queue.BULK)
            raise RuntimeError("the import blew up partway through")

        enqueue.assert_called_once_with(BATCH_TASK, [1], queue=Queue.BULK)

    def test_nesting_reuses_the_outer_collector(self) -> None:
        """A helper that also opens this context must not fragment the outer caller's own chunks."""
        with mock.patch(ENQUEUE) as enqueue, batching_follow_on_work(chunk_size=5):
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 1, queue=Queue.BULK)
            with batching_follow_on_work(chunk_size=1):
                enqueue_follow_on(ITEM_TASK, BATCH_TASK, 2, queue=Queue.BULK)
            enqueue.assert_not_called()

        enqueue.assert_called_once_with(BATCH_TASK, [1, 2], queue=Queue.BULK)

    def test_two_task_families_batch_independently_inside_one_context(self) -> None:
        with mock.patch(ENQUEUE) as enqueue, batching_follow_on_work(chunk_size=2):
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 1, queue=Queue.BULK)
            enqueue_follow_on(OTHER_ITEM_TASK, OTHER_BATCH_TASK, 100, queue=Queue.BULK)
            enqueue.assert_not_called()
            enqueue_follow_on(ITEM_TASK, BATCH_TASK, 2, queue=Queue.BULK)

            enqueue.assert_called_once_with(BATCH_TASK, [1, 2], queue=Queue.BULK)

    def test_the_default_chunk_size_is_used_when_none_is_given(self) -> None:
        with mock.patch(ENQUEUE) as enqueue, batching_follow_on_work():
            for item_id in range(DEFAULT_CHUNK_SIZE):
                enqueue_follow_on(ITEM_TASK, BATCH_TASK, item_id, queue=Queue.BULK)

        enqueue.assert_called_once_with(BATCH_TASK, list(range(DEFAULT_CHUNK_SIZE)), queue=Queue.BULK)
