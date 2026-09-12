"""Run callables against the database at genuinely the same time."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from django.db import connections

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Long enough that a slow container start doesn't fail a passing test, short
#: enough that a genuine deadlock surfaces as a failure rather than a hang.
DEFAULT_TIMEOUT_SECONDS = 30


def run_concurrently(callables: Sequence[Callable[[], Any]], *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[Any]:
    """Run every callable on its own thread, released together at a barrier.

    Each thread closes its database connections on the way out; leaking them keeps the test database busy and
    makes teardown hang.

    Args:
        callables: The work to run simultaneously.
        timeout: Seconds to wait, both at the barrier and when joining.

    Returns:
        Each callable's return value, in the order given.

    Raises:
        AssertionError: A thread raised, or did not finish within *timeout*."""
    barrier = threading.Barrier(len(callables), timeout=timeout)
    results: list[Any] = [None] * len(callables)
    failures: list[BaseException] = []

    def runner(index: int, work: Callable[[], Any]) -> None:
        try:
            # Everyone is inside their call before anyone touches the database.
            barrier.wait()
            results[index] = work()
        except BaseException as exc:
            failures.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(index, work)) for index, work in enumerate(callables)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)

    still_running = [thread for thread in threads if thread.is_alive()]
    if still_running:
        raise AssertionError(
            f"{len(still_running)} of {len(threads)} threads did not finish within {timeout}s - likely a deadlock or a lock held across the barrier"
        )
    if failures:
        raise AssertionError(f"concurrent work raised: {failures!r}")
    return results
