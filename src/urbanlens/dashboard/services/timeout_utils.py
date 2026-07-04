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

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Shared across all callers: bounding a slow call just abandons its thread
# (Python cannot forcibly kill a blocked thread), so a small dedicated pool
# keeps abandoned calls from accumulating unbounded background threads.
_EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="ext-api-deadline")


def call_with_deadline[T](func: Callable[[], T], *, timeout: float, default: T) -> T:
    """Run ``func`` with a hard wall-clock deadline, returning ``default`` on timeout.

    Args:
        func: Zero-argument callable to run (wrap a gateway call in a lambda).
        timeout: Maximum seconds to wait for ``func`` to complete.
        default: Value to return if ``func`` exceeds ``timeout`` or raises.

    Returns:
        The result of ``func``, or ``default`` when it times out or errors.
    """
    future = _EXECUTOR.submit(func)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        logger.warning("External call exceeded %.0fs deadline -- abandoning it in the background", timeout)
        return default
    except Exception:
        logger.exception("External call raised inside deadline wrapper")
        return default
