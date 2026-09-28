"""Shared Celery helpers for queueing work and reporting progress."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from celery import current_app, current_task
from celery.result import AsyncResult
from kombu.exceptions import KombuError

from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import Model, QuerySet

logger = logging.getLogger(__name__)

PROGRESS_STATE = "PROGRESS"

#: What ``apply_async`` raises when the broker cannot take a message.
BROKER_ERRORS: tuple[type[Exception], ...] = (KombuError, ConnectionError, OSError, RuntimeError)

#: Queues whose jobs are sized by what one account owns.
_BATCH_QUEUES = frozenset({Queue.BULK, Queue.MAINTENANCE, Queue.SANDBOX_BATCH, Queue.DEFAULT})


def follow_on_queue() -> str | None:
    """The queue for work a model signal enqueues, given the task (if any) that caused the save.

    A signal fires once per saved row, so an import queues its per-row work once per row. From inside a
    batch job that work goes to ``bulk``, rather than to the interactive worker the safety check-ins share.

    Returns:
        ``Queue.BULK`` inside a task delivered from, or declared on, a batch queue; otherwise None, which
        keeps the enqueued task's own queue.
    """
    if not current_task:
        return None
    delivered = (current_task.request.delivery_info or {}).get("routing_key")
    return Queue.BULK if (delivered or getattr(current_task, "queue", None)) in _BATCH_QUEUES else None


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Serializable task status payload for progress-bar UIs."""

    task_id: str
    state: str
    current: int = 0
    total: int = 1
    percent: int = 0
    message: str = ""
    result: Any | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "current": self.current,
            "total": self.total,
            "percent": self.percent,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "ready": self.state in {"SUCCESS", "FAILURE", "REVOKED"},
        }


def update_task_progress(task: Any, *, current: int, total: int, message: str = "") -> None:
    """Update Celery task metadata in a consistent progress format.
    Best-effort, and deliberately broad in what it swallows, matching ``channel_broadcast.send_group_message``'s "never raises" contract.

    Args:
        task: The bound task instance (``self`` in a ``bind=True`` task).
        current: Items completed so far.
        total: Total items; coerced to at least 1 so the percentage is always defined.
        message: Human-readable status line for polling clients."""
    safe_total = max(int(total or 1), 1)
    safe_current = max(0, min(int(current or 0), safe_total))
    percent = int((safe_current / safe_total) * 100)
    try:
        task.update_state(
            state=PROGRESS_STATE,
            meta={
                "current": safe_current,
                "total": safe_total,
                "percent": percent,
                "message": message,
            },
        )
    except Exception:
        logger.warning("Could not report progress for task %s", getattr(task, "name", task), exc_info=True)


def get_task_progress(task_id: str) -> TaskProgress:
    """Return normalized task status for polling clients."""
    result = AsyncResult(task_id, app=current_app)
    state = result.state
    info = result.info if isinstance(result.info, dict) else {}

    if state == "SUCCESS":
        return TaskProgress(task_id=task_id, state=state, current=1, total=1, percent=100, result=result.result)
    if state in {"FAILURE", "REVOKED"}:
        error = str(result.result or result.info or "Task failed")
        return TaskProgress(task_id=task_id, state=state, error=error)

    current = int(info.get("current") or 0)
    total = int(info.get("total") or 1)
    percent = int(info.get("percent") or 0)
    message = str(info.get("message") or "")
    return TaskProgress(task_id=task_id, state=state, current=current, total=total, percent=percent, message=message)


def safely_enqueue_task(
    task: Any,
    *args: Any,
    countdown: int | None = None,
    queue: str | None = None,
    expires: int | None = None,
    durable: bool = True,
    **kwargs: Any,
) -> AsyncResult | None:
    """Queue a Celery task with consistent logging and broker exception handling.

    A durable enqueue the broker refuses is written to the task outbox instead, in the caller's transaction,
    and ``tasks.drain_task_outbox`` queues it once the broker is back. A caller that inspects the result and
    handles ``None`` itself (reporting the failure, running inline, releasing a claim) must pass
    ``durable=False``, or the work would happen twice; ``bin/check_enqueue_durability.py`` requires any caller
    that uses the result to say which it wants.

    Args:
        task: The Celery task to enqueue.
        *args: Positional arguments passed to the task.
        countdown: Seconds to delay execution, if any.
        queue: Celery queue to dispatch to; None uses the task's default route.
        expires: Seconds from now after which the broker should drop this task unexecuted, rather than run it late.
        durable: Whether a refused enqueue is kept in the outbox and retried.
        **kwargs: Keyword arguments passed to the task.

    Returns:
        The AsyncResult on success, or None when the broker was unreachable (a durable enqueue is then pending in
        the outbox)."""
    try:
        apply_kwargs: dict[str, Any] = {}
        if countdown is not None:
            apply_kwargs["countdown"] = countdown
        if queue is not None:
            apply_kwargs["queue"] = queue
        if expires is not None:
            apply_kwargs["expires"] = expires
        return task.apply_async(args=args, kwargs=kwargs, **apply_kwargs)
    except BROKER_ERRORS:
        logger.exception("Unable to enqueue Celery task %s", getattr(task, "name", task))
        if durable:
            from urbanlens.dashboard.services.core.task_outbox import record_refused_enqueue

            record_refused_enqueue(task, args, kwargs, countdown=countdown, queue=queue, expires=expires)
        return None


def pk_ranges(queryset: QuerySet[Model], chunk_size: int) -> Iterator[tuple[int, int]]:
    """Consecutive ``(first, last)`` primary-key bounds covering ``queryset``, ``chunk_size`` rows at a time.

    Pages by keyset (``pk > last``), so memory stays at one chunk of keys however large the table is.

    Args:
        queryset: The rows to cover; its own filters decide which keys count.
        chunk_size: Most rows per range.

    Yields:
        Inclusive bounds of each range, in key order.
    """
    chunk_size = max(1, chunk_size)
    ordered = queryset.order_by("pk")
    last: int | None = None
    while True:
        page = ordered if last is None else ordered.filter(pk__gt=last)
        keys = list(page.values_list("pk", flat=True)[:chunk_size])
        if not keys:
            return
        yield keys[0], keys[-1]
        last = keys[-1]


def dispatch_pk_ranges(queryset: QuerySet[Model], task: Any, *args: Any, chunk_size: int, durable: bool = True) -> int:
    """Queue ``task(*args, first_pk, last_pk)`` once per :func:`pk_ranges` range of ``queryset``.

    Args:
        queryset: The rows to cover.
        task: A task taking the range bounds as its last two arguments.
        *args: Arguments before the bounds.
        chunk_size: Most rows per range.
        durable: Passed to :func:`safely_enqueue_task`.

    Returns:
        How many ranges were queued.
    """
    dispatched = 0
    for first, last in pk_ranges(queryset, chunk_size):
        if safely_enqueue_task(task, *args, first, last, durable=durable) is not None:
            dispatched += 1
    return dispatched
