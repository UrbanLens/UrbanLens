"""Utilities for bounding the wall-clock time of blocking external calls.
``call_with_deadline`` runs the call in a worker thread and gives up waiting after a fixed wall-clock budget, so a view can never be held hostage by one slow provider."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import logging
from typing import TYPE_CHECKING

from django.db import close_old_connections

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Shared wall-clock budget (seconds) for external calls made inside a synchronous request handler.
EXTERNAL_CALL_DEADLINE: float = 20.0

# Shared across all callers: bounding a slow call just abandons its thread (Python cannot forcibly
# kill a blocked thread), so a small dedicated pool keeps abandoned calls from accumulating
# unbounded background threads.
_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="ext-api-deadline")


def call_with_deadline[T](func: Callable[[], T], *, timeout: float, default: T, name: str | None = None) -> T:
    """Run ``func`` with a hard wall-clock deadline, returning ``default`` only on timeout.

    Args:
        func: Zero-argument callable to run (wrap a gateway call in a lambda).
        timeout: Maximum seconds to wait for ``func`` to complete.
        default: Value to return if ``func`` exceeds ``timeout``.
        name: Short label (e.g. the gateway's service key) used in log messages, so a timeout in production identifies which provider was slow instead of logging anonymously.

    Returns:
        The result of ``func``, or ``default`` when it times out.

    Raises:
        Exception: Whatever ``func`` itself raises (other than a timeout)."""
    label = name or getattr(func, "__qualname__", repr(func))

    def _run() -> T:
        try:
            return func()
        finally:
            # Executor threads live for the life of the process and Django DB connections are
            # thread-local: a callable that touches the ORM (e.g. writing LocationCache from inside
            # the deadline) would otherwise leave an idle connection pinned to this pool slot
            # indefinitely.
            close_old_connections()

    future = _EXECUTOR.submit(_run)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        # cancel() only succeeds while the future is still queued -- i.e. the call never started
        # because every executor slot was busy for the entire deadline.
        # Distinguishing that from a slow upstream matters when reading production logs: the former
        # means *this process* is saturated, the latter blames the provider named in the label.
        if future.cancel():
            logger.warning("External call %r timed out after %.0fs without ever starting -- deadline executor saturated", label, timeout)
        else:
            logger.warning("External call %r exceeded %.0fs deadline -- abandoning it in the background", label, timeout)
        return default
