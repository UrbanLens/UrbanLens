"""Whether the code running now is work nobody is waiting on, so a struggling provider can stop it first.

The queue a task runs on already says who waits (``services.sandbox.queues``): ``interactive``, ``panel_fetch``,
``ai`` and ``sandbox`` are a person waiting on a result; ``bulk``, ``maintenance``, ``sandbox_batch`` and Celery's
default queue are not. Being inside a task says nothing by itself, because a page's panels are fetched by tasks
on ``panel_fetch``. ``UrbanLensTask`` binds the answer for each run; outside any task - a request, a WebSocket
consumer, a management command someone is watching - the work is live.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Queues whose tasks nobody is waiting on.
BACKGROUND_QUEUES: frozenset[str] = frozenset({Queue.BULK, Queue.MAINTENANCE, Queue.DEFAULT, Queue.SANDBOX_BATCH})

_BACKGROUND: ContextVar[bool] = ContextVar("ul_background_work", default=False)


def is_background() -> bool:
    """Whether the current work is background work.

    Returns:
        True inside a task on one of :data:`BACKGROUND_QUEUES`, or a :func:`background_work` block.
    """
    return _BACKGROUND.get()


def queue_is_background(queue: str | None) -> bool:
    """Whether a task on ``queue`` is background work.

    Args:
        queue: The queue name, or None when it is not known.

    Returns:
        True for one of :data:`BACKGROUND_QUEUES`. An unknown queue is treated as live.
    """
    return queue in BACKGROUND_QUEUES


@contextmanager
def background_work(enabled: bool = True) -> Iterator[None]:
    """Mark the enclosed block as background work, or as live with ``enabled=False``.

    Args:
        enabled: Whether the block is background work.

    Yields:
        Nothing.
    """
    token = _BACKGROUND.set(enabled)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)
