"""Run selected Celery tasks inline, for tests whose subject *is* the task's effect.

Most tests want an enqueued task to stay enqueued: they assert a request
returned quickly and handed work off, and stub ``safely_enqueue_task`` to check
that it did. A few are the opposite - the thing under test only becomes true
once the task runs:

* A consumer broadcast. ``services.core.channel_broadcast.send_group_message``
  does not call ``group_send`` itself; it enqueues
  ``tasks.broadcast_channel_group_message`` so the ``async_to_sync`` hop
  happens on the prefork worker rather than inside a gunicorn gevent greenlet
  (see that task's docstring and docs/PROBLEMS.md's gevent/asyncio entry).
* A prewarmed game round. ``get_or_create_round`` enqueues the prewarm for the
  *next* round, and the test's whole point is that the next round then comes
  from cache instead of being generated live.

With no worker draining the broker, those tasks are silently dropped and the
assertion can never hold - which reads as a bug in the feature rather than a
missing runner.

``CELERY_TASK_ALWAYS_EAGER`` would fix all of them at once and is what several
of these tests' own docstrings assume, but it is far too blunt in practice:
turning it on runs *every* task a test incidentally triggers, and merely
creating a profile fans out into label enrichment that then trips the outbound
rate limiter. Selecting the one or two tasks a test actually depends on keeps
the blast radius to the behaviour being asserted.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest import mock

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def tasks_run_inline(*tasks) -> Iterator[mock.MagicMock]:
    """Execute the given Celery tasks synchronously when they are enqueued.

    Any other task enqueued while this is active is recorded and skipped,
    exactly as it would be with no worker running.

    Args:
        *tasks: The task callables to run inline (e.g.
            ``tasks.broadcast_channel_group_message``).

    Yields:
        The patched ``safely_enqueue_task`` mock, so a test can additionally
        assert on what else was enqueued.
    """
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

    The common case of :func:`tasks_run_inline` - see this module's docstring
    for why a consumer test needs it.

    Yields:
        The patched ``safely_enqueue_task`` mock.
    """
    from urbanlens.dashboard.tasks import broadcast_channel_group_message

    with tasks_run_inline(broadcast_channel_group_message) as enqueue:
        yield enqueue
