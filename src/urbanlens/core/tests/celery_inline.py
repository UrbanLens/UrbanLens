"""Run selected Celery tasks inline, for tests whose subject *is* the task's effect."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest import mock

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def tasks_run_inline(*tasks) -> Iterator[mock.MagicMock]:
    """Execute the given Celery tasks synchronously when they are enqueued.

    Any other task enqueued while this is active is recorded and skipped, exactly as it would be with no worker
    running.

    Args:
        *tasks: The task callables to run inline (e.g.

    Yields:
        The patched ``safely_enqueue_task`` mock, so a test can additionally assert on what else was enqueued."""
    selected = set(tasks)

    def _dispatch(task, *args, **kwargs):
        # `queue` is routing metadata for the broker, not an argument the task
        # body takes.
        kwargs.pop("queue", None)
        if task in selected:
            return task(*args, **kwargs)
        return None

    with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", side_effect=_dispatch) as enqueue:
        yield enqueue


@contextmanager
def broadcasts_delivered_inline() -> Iterator[mock.MagicMock]:
    """Run channel-layer broadcast tasks immediately instead of enqueueing them.

    The common case of :func:`tasks_run_inline` - see this module's docstring for why a consumer test needs it.

    Yields:
        The patched ``safely_enqueue_task`` mock."""
    from urbanlens.dashboard.tasks import broadcast_channel_group_message

    with tasks_run_inline(broadcast_channel_group_message) as enqueue:
        yield enqueue
