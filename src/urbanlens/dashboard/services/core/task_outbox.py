"""The task outbox: enqueues the broker refused, kept in the database and queued again once it recovers.

``safely_enqueue_task`` records one here only on failure, so the healthy path costs nothing. The entry is
written in the caller's transaction: a change that rolls back takes its follow-up work with it, and one
that commits keeps it even though the broker never saw the message.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.services.core.celery import BROKER_ERRORS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

#: Entries queued per drain run; the drain runs every minute.
DRAIN_BATCH = 500

#: Longest wait between attempts for one entry while the broker stays down.
MAX_BACKOFF = timedelta(minutes=15)


def record_refused_enqueue(task: Any, args: Sequence[Any], kwargs: Mapping[str, Any], *, countdown: int | None, queue: str | None, expires: int | None) -> bool:
    """Keep an enqueue the broker refused, for :func:`drain_outbox` to queue later.

    Never raises: the caller has already lost the enqueue, and failing its request as well would make an
    outage worse. A write that cannot happen (no database, a broken transaction, an async context) is logged.

    Args:
        task: The Celery task that could not be queued.
        args: Its positional arguments.
        kwargs: Its keyword arguments.
        countdown: The delay the caller asked for, in seconds.
        queue: The queue the caller named, or None for the task's own.
        expires: Seconds after which the caller wanted it dropped rather than run late.

    Returns:
        Whether the entry was written.
    """
    from urbanlens.dashboard.models.task_outbox import TaskOutboxEntry

    task_name = getattr(task, "name", None)
    if not isinstance(task_name, str) or not task_name:
        logger.error("Cannot keep a refused enqueue of %r: it has no task name", task)
        return False
    now = timezone.now()
    try:
        with transaction.atomic():
            TaskOutboxEntry.objects.create(
                task_name=task_name,
                args=list(args),
                kwargs=dict(kwargs),
                queue=queue or "",
                not_before=now + timedelta(seconds=countdown) if countdown else None,
                expires_at=now + timedelta(seconds=expires) if expires else None,
                next_attempt_at=now,
            )
    except Exception:
        logger.exception("Could not keep the refused enqueue of %s in the outbox; it is lost", task_name)
        return False
    return True


def _backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** min(attempts, 10)), MAX_BACKOFF)


def drain_outbox(*, batch: int = DRAIN_BATCH) -> int:
    """Queue every due outbox entry, deleting each once the broker takes it.

    Stops at the first refusal, pushing that entry's next attempt back: the rest would be refused too.
    An entry whose ``expires`` has passed, or whose task no longer exists, is dropped.

    Args:
        batch: Most entries to handle in this run.

    Returns:
        How many entries were queued.
    """
    from celery import current_app

    from urbanlens.dashboard.models.task_outbox import TaskOutboxEntry

    now = timezone.now()
    queued = 0
    for entry in TaskOutboxEntry.objects.due(now)[:batch]:
        if entry.expires_at is not None and entry.expires_at <= now:
            logger.warning("Dropping outbox entry %s for %s: it expired before the broker took it", entry.pk, entry.task_name)
            entry.delete()
            continue
        task = current_app.tasks.get(entry.task_name)
        if task is None:
            logger.error("Dropping outbox entry %s: no task is registered as %s", entry.pk, entry.task_name)
            entry.delete()
            continue
        options: dict[str, Any] = {}
        if entry.not_before is not None and entry.not_before > now:
            options["countdown"] = math.ceil((entry.not_before - now).total_seconds())
        if entry.expires_at is not None:
            options["expires"] = entry.expires_at
        if entry.queue:
            options["queue"] = entry.queue
        try:
            task.apply_async(args=entry.args, kwargs=entry.kwargs, **options)
        except BROKER_ERRORS as exc:
            entry.attempts += 1
            entry.next_attempt_at = now + _backoff(entry.attempts)
            entry.last_error = str(exc)[:255]
            entry.save(update_fields=["attempts", "next_attempt_at", "last_error", "updated"])
            logger.warning("The broker still refuses queued work; outbox entry %s waits until %s", entry.pk, entry.next_attempt_at)
            break
        entry.delete()
        queued += 1
    if queued:
        logger.info("Queued %d task(s) the broker had refused", queued)
    return queued
