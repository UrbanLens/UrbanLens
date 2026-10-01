"""Bounds how many discrete Celery tasks one bulk action's per-row signals can create.

A signal that queues one follow-on task per saved row (P109) turns an N-row bulk action into N
broker messages sharing a FIFO queue with every other account's bulk work, drained at that queue's
own low concurrency - one account's import can hold another account's job behind thousands of rows
for hours, none of which even belong to it. Wrapping the bulk action's per-row loop in
:func:`batching_follow_on_work` buffers those ids and flushes bounded chunks instead, so the queue
depth one action can create tracks ``rows / chunk_size``, not ``rows``. See docs/archive/PROBLEMS-ARCHIVE.md P109.

Outside that context, :func:`enqueue_follow_on` enqueues exactly as ``safely_enqueue_task`` always
has - a signal fired by someone's own single save is unaffected.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.core.celery import safely_enqueue_task

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Ids buffered per follow-on task before a chunk is flushed early. A follow-on task whose own
#: per-item cost is dominated by a rate-limited external call (wiki enrichment, category
#: suggestion) stays bounded in wall-clock time at this size even though its total item count is
#: whatever one bulk action produced.
DEFAULT_CHUNK_SIZE = 50


@dataclass(slots=True)
class _PendingChunk:
    """One follow-on task's buffered ids, and how to flush them."""

    batch_task: Any
    queue: str | None
    ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class BulkFollowUpCollector:
    """Buffers follow-on work ids per batch task, flushing bounded chunks rather than one per id.

    Attributes:
        chunk_size: Ids buffered per batch task before an early flush.
    """

    chunk_size: int = DEFAULT_CHUNK_SIZE
    _pending: dict[str, _PendingChunk] = field(default_factory=dict)

    def add(self, batch_task: Any, item_id: int, *, queue: str | None) -> None:
        """Buffer *item_id* for *batch_task*, flushing early once a chunk fills.

        Args:
            batch_task: The chunk-shaped task to eventually enqueue - takes one positional
                argument, a list of ids.
            item_id: The id to buffer.
            queue: The queue the flushed chunk should be enqueued on.
        """
        pending = self._pending.setdefault(batch_task.name, _PendingChunk(batch_task=batch_task, queue=queue))
        pending.ids.append(item_id)
        if len(pending.ids) >= self.chunk_size:
            self._flush(batch_task.name)

    def _flush(self, task_name: str) -> None:
        pending = self._pending.get(task_name)
        if not pending or not pending.ids:
            return
        chunk, pending.ids = pending.ids, []
        safely_enqueue_task(pending.batch_task, chunk, queue=pending.queue)

    def flush_all(self) -> None:
        """Enqueue every batch task's remaining partial chunk."""
        for task_name in list(self._pending):
            self._flush(task_name)


_active: ContextVar[BulkFollowUpCollector | None] = ContextVar("bulk_followup_collector", default=None)


@contextmanager
def batching_follow_on_work(chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[None]:
    """Buffer this block's :func:`enqueue_follow_on` calls into bounded chunks.

    Flushes on the way out even when the block raised, so a bulk action that fails partway through
    still queues the follow-on work for the rows it did finish rather than dropping it silently.

    Nesting reuses the outermost collector rather than starting a second one, so a helper that also
    opens this context does not fragment the outer caller's own chunks.

    Args:
        chunk_size: Ids buffered per batch task before an early flush. Ignored when already inside
            an active collector - the outermost caller's setting wins.
    """
    if _active.get() is not None:
        yield
        return
    collector = BulkFollowUpCollector(chunk_size=chunk_size)
    token = _active.set(collector)
    try:
        yield
    finally:
        _active.reset(token)
        collector.flush_all()


def enqueue_follow_on(item_task: Any, batch_task: Any, item_id: int, *, queue: str | None) -> None:
    """Enqueue one row's follow-on work, buffered into a bounded chunk when a collector is active.

    Args:
        item_task: The per-item task a caller outside any collector enqueues directly - unaffected
            by this function when no :func:`batching_follow_on_work` block is open.
        batch_task: Its chunk-shaped sibling, taking one list-of-ids argument.
        item_id: The id to enqueue.
        queue: The queue to enqueue on.
    """
    collector = _active.get()
    if collector is None:
        safely_enqueue_task(item_task, item_id, queue=queue)
        return
    collector.add(batch_task, item_id, queue=queue)
