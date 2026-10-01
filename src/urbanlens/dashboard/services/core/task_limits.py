"""Time limits for Celery tasks: a default per queue, a ceiling per queue, and a soft limit ``except Exception`` cannot swallow.

A task that declares no limit inherits its queue's default through :class:`QueueTimeLimits` (the
``task_annotations`` setting), so an interactive task no longer inherits the hour-long global limit.
``dashboard.checks.check_every_task_has_a_time_limit`` fails startup for a task whose limits are missing,
inverted, or above its queue's ceiling.

Billiard raises ``SoftTimeLimitExceeded``, an ``Exception``, from a signal handler, so every broad
``except Exception`` between the task and the code running when it fires swallows it and the task carries on
until the hard kill. :class:`UrbanLensTask` swaps that handler, for the duration of each task, for one raising
:class:`TaskSoftTimeLimit`, a ``BaseException``, which passes those handlers the way ``KeyboardInterrupt`` does, and converts it back to
``SoftTimeLimitExceeded`` at the task boundary so Celery records an ordinary failure. Code that wants to clean
up on a soft limit catches :data:`SOFT_TIME_LIMIT_ERRORS`.
"""

from __future__ import annotations

from contextlib import contextmanager
import signal
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import FrameType


class TimeLimits(NamedTuple):
    """A task's soft and hard time limits, in seconds."""

    soft: int
    hard: int


def queue_defaults() -> dict[str, TimeLimits]:
    """The limits a task on each queue gets when it declares none.

    Returns:
        Limits keyed by queue name. A queue missing here uses the global ``CELERY_TASK_*_TIME_LIMIT``.
    """
    return {
        Queue.INTERACTIVE: TimeLimits(120, 150),
        Queue.PANEL_FETCH: TimeLimits(110, 130),
        Queue.AI: TimeLimits(90, 120),
        # A video transcode waits up to ten minutes on ffmpeg (services/media/videos.py).
        Queue.SANDBOX: TimeLimits(720, 780),
        # Under the 3300-second overlap locks the hourly sweeps take.
        Queue.MAINTENANCE: TimeLimits(2700, 3000),
    }


def queue_ceilings() -> dict[str, int]:
    """The longest hard limit a task on each queue may declare.

    Returns:
        Ceilings keyed by queue name. Every other queue is capped by the broker's visibility timeout.
    """
    return {
        Queue.INTERACTIVE: 300,
        Queue.PANEL_FETCH: 300,
        Queue.AI: 180,
        Queue.SANDBOX: 900,
        Queue.MAINTENANCE: 3300,
    }


def global_limits() -> TimeLimits:
    """The global limits from settings, used by queues without their own default."""
    from django.conf import settings

    return TimeLimits(settings.CELERY_TASK_SOFT_TIME_LIMIT, settings.CELERY_TASK_TIME_LIMIT)


def ceiling_for(queue: str) -> int:
    """The longest hard limit allowed on ``queue``.

    Args:
        queue: A queue name.

    Returns:
        The queue's ceiling, or the broker visibility timeout less ten minutes, past which the broker would
        redeliver a task that is still running.
    """
    from django.conf import settings

    visibility = int(settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"])
    return queue_ceilings().get(str(queue), visibility - 600)


class QueueTimeLimits:
    """A ``task_annotations`` entry giving each task its queue's default limits, unless it declares its own."""

    def annotate(self, task: Task) -> dict[str, int] | None:
        """The limits to set on ``task``.

        Args:
            task: A task class being bound to the app.

        Returns:
            ``soft_time_limit``/``time_limit`` for whichever the task left unset, or None when it set both.
        """
        limits = queue_defaults().get(str(getattr(task, "queue", None) or ""), None) or global_limits()
        annotation: dict[str, int] = {}
        if task.soft_time_limit is None:
            annotation["soft_time_limit"] = limits.soft
        if task.time_limit is None:
            annotation["time_limit"] = max(limits.hard, annotation.get("soft_time_limit", task.soft_time_limit or 0) + 1)
        return annotation or None

    def annotate_any(self) -> None:
        """No annotation applies to every task regardless of name."""
        return


class TaskSoftTimeLimit(BaseException):
    """A task's soft time limit, raised past ``except Exception`` so the task stops rather than carrying on."""


#: Catch these to clean up on a soft limit, inside a task or in code a test calls directly.
SOFT_TIME_LIMIT_ERRORS: tuple[type[BaseException], ...] = (TaskSoftTimeLimit, SoftTimeLimitExceeded)


def _raise_soft_limit(signum: int, frame: FrameType | None) -> None:
    raise TaskSoftTimeLimit


@contextmanager
def soft_limit_escapes_broad_handlers() -> Iterator[None]:
    """Raise :class:`TaskSoftTimeLimit` instead of ``SoftTimeLimitExceeded`` for the duration of the block.

    Only in a prefork child's main thread, where billiard installed its own handler; anywhere else (a web
    process, a thread pool, an eager test) the signal is left as it is. Billiard's handler is restored on the
    way out, so a limit that fires in Celery's own bookkeeping after the task body is reported as before.

    Yields:
        Nothing.
    """
    from billiard import pool

    signum = pool.SIG_SOFT_TIMEOUT
    replaced = signum is not None and threading.current_thread() is threading.main_thread() and signal.getsignal(signum) is pool.soft_timeout_sighandler
    if replaced:
        signal.signal(signum, _raise_soft_limit)
    try:
        yield
    finally:
        if replaced:
            signal.signal(signum, pool.soft_timeout_sighandler)


class UrbanLensTask(Task):
    """The base class of every task: a soft limit escapes broad handlers inside it, and is reported as a failure."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            with soft_limit_escapes_broad_handlers():
                return super().__call__(*args, **kwargs)
        except TaskSoftTimeLimit as exc:
            raise SoftTimeLimitExceeded(f"{self.name} exceeded its soft time limit of {self.soft_time_limit}s") from exc
