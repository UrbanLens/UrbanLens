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

The patch target is the subtle part. Every caller binds the name into its own
module at import (``from ... import safely_enqueue_task``), so patching where the
function is *defined* replaces an attribute none of them read: the real enqueue
runs, the task goes to a broker with no worker, and the test waits for something
that will never arrive. When the waiting is done by a `WebsocketCommunicator`,
that surfaces as `TimeoutError` on a socket - which reads as a slow host rather
than a broken fixture, and was mistaken for one.

``CELERY_TASK_ALWAYS_EAGER`` would fix all of them at once and is what several
of these tests' own docstrings assume, but it is far too blunt in practice:
turning it on runs *every* task a test incidentally triggers, and merely
creating a profile fans out into label enrichment that then trips the outbound
rate limiter. Selecting the one or two tasks a test actually depends on keeps
the blast radius to the behaviour being asserted.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import sys
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
    from urbanlens.dashboard.services.core import celery as celery_module

    selected = set(tasks)

    def _dispatch(task, *args, **kwargs):
        # `queue` is routing metadata for the broker, not an argument the task
        # body takes.
        kwargs.pop("queue", None)
        if task in selected:
            return task(*args, **kwargs)
        return None

    original = celery_module.safely_enqueue_task
    # Patching where the function is defined rebinds an attribute nobody reads:
    # every caller does `from ... import safely_enqueue_task`, which copies the
    # reference into its own module at import. Resolved from sys.modules rather
    # than listed, so a module that starts importing it later is covered without
    # anyone remembering to add it here.
    holders = [
        module for module in list(sys.modules.values()) if getattr(module, "safely_enqueue_task", None) is original
    ]

    enqueue = mock.MagicMock(side_effect=_dispatch)
    try:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(celery_module, "safely_enqueue_task", enqueue))
            for module in holders:
                if module is not celery_module:
                    stack.enter_context(mock.patch.object(module, "safely_enqueue_task", enqueue))
            yield enqueue
    finally:
        # A module imported for the first time *inside* the block binds whatever
        # the celery module held at that moment, which is the mock - and
        # mock.patch restores only the modules it was handed. Three of the
        # holders are not imported at Django startup, so this is reachable, and
        # the symptom would be the same silent one this helper's own bug had:
        # every later enqueue through that module quietly doing nothing.
        for module in list(sys.modules.values()):
            if getattr(module, "safely_enqueue_task", None) is enqueue:
                module.safely_enqueue_task = original


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
