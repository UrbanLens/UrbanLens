"""Run callables against the database at genuinely the same time.

Every lost-update fixed during the 2026-08-17 audit - the pay-what-you-want
ledger, the Stripe sync, wiki edits, settings forms, the map quick-edit, round
ratings - was a read-modify-write that only misbehaves when two writers overlap.
Most of them can be demonstrated by driving two stale snapshots in sequence, but
a *lock* cannot: `select_for_update` does nothing observable on one connection,
so proving it works needs real threads on real connections.

That means ``TransactionTestCase`` rather than the project's usual ``TestCase``:
the threads have to see each other's committed rows, which a single wrapping
transaction hides.

One trap this encodes, learned the hard way. A race test seeded with a
brand-new parent row exercises ``get_or_create``'s *insert* path, where the
unique index blocks the second thread until the first commits - serialising the
threads by accident and hiding the very defect under test. The damaging path is
the ordinary one, where the row already exists and both threads merely SELECT
it. Seed the row first; :func:`run_concurrently` cannot check that for you.
"""

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

    Each thread closes its database connections on the way out; leaking them
    keeps the test database busy and makes teardown hang.

    Args:
        callables: The work to run simultaneously. Two is the usual case.
        timeout: Seconds to wait, both at the barrier and when joining.

    Returns:
        Each callable's return value, in the order given.

    Raises:
        AssertionError: A thread raised, or did not finish within *timeout*.
            The original exceptions are attached to the message, since a race
            that errors is a different bug from a race that corrupts.
    """
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
        raise AssertionError(f"{len(still_running)} of {len(threads)} threads did not finish within {timeout}s - likely a deadlock or a lock held across the barrier")
    if failures:
        raise AssertionError(f"concurrent work raised: {failures!r}")
    return results
