"""Utilities for bounding the wall-clock time of blocking external calls.

``requests``' own ``timeout=`` parameter only bounds inactivity between
socket reads (or between connect attempts) -- a slow trickle of bytes from
an upstream API can still keep a request handler blocked far longer than any
single per-call timeout would suggest. ``call_with_deadline`` runs the call
in a worker thread and gives up waiting after a fixed wall-clock budget, so
a view can never be held hostage by one slow provider.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import logging
from typing import TYPE_CHECKING

from django.db import close_old_connections

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Shared wall-clock budget (seconds) for external calls made inside a
#: synchronous request handler. Gateways invoked under this deadline should
#: keep their own per-request ``timeout=`` at or below it -- a result that
#: arrives after the deadline is discarded (or, for callables that cache
#: internally, only benefits the *next* request), so a longer inner timeout
#: buys nothing except an executor slot tied up on work nobody is waiting for.
EXTERNAL_CALL_DEADLINE: float = 20.0

# Shared across all callers: bounding a slow call just abandons its thread
# (Python cannot forcibly kill a blocked thread), so a small dedicated pool
# keeps abandoned calls from accumulating unbounded background threads. The
# external-data panels now fetch in Celery (services/external_data.py), so the
# remaining request-path users are rare: the web-search fetch and the
# satellite/street cache replays. Still sized generously because an abandoned
# call occupies its slot until the underlying network call completes or errors.
_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="ext-api-deadline")


def call_with_deadline[T](func: Callable[[], T], *, timeout: float, default: T, name: str | None = None) -> T:
    """Run ``func`` with a hard wall-clock deadline, returning ``default`` on timeout.

    Args:
        func: Zero-argument callable to run (wrap a gateway call in a lambda).
        timeout: Maximum seconds to wait for ``func`` to complete.
        default: Value to return if ``func`` exceeds ``timeout`` or raises.
        name: Short label (e.g. the gateway's service key) used in log
            messages, so a timeout in production identifies which provider
            was slow instead of logging anonymously.

    Returns:
        The result of ``func``, or ``default`` when it times out or errors.
    """
    label = name or getattr(func, "__qualname__", repr(func))

    def _run() -> T:
        try:
            return func()
        finally:
            # Executor threads live for the life of the process and Django DB
            # connections are thread-local: a callable that touches the ORM
            # (e.g. writing LocationCache from inside the deadline) would
            # otherwise leave an idle connection pinned to this pool slot
            # indefinitely.
            close_old_connections()

    future = _EXECUTOR.submit(_run)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        # cancel() only succeeds while the future is still queued -- i.e. the
        # call never started because every executor slot was busy for the
        # entire deadline. Distinguishing that from a slow upstream matters
        # when reading production logs: the former means *this process* is
        # saturated, the latter blames the provider named in the label.
        if future.cancel():
            logger.warning("External call %r timed out after %.0fs without ever starting -- deadline executor saturated", label, timeout)
        else:
            logger.warning("External call %r exceeded %.0fs deadline -- abandoning it in the background", label, timeout)
        return default
    except Exception:
        logger.exception("External call %r raised inside deadline wrapper", label)
        return default
