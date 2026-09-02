"""Turn lifecycle primitives shared by the web view, the external API, and the task itself.

An assistant "turn" spans two processes: a web request (or external API
call) starts it and returns immediately with a turn id; ``ai-worker``
(``run_assistant_turn_task``, added alongside the native-tool-calling loop
rewrite) executes it later and writes its result to the Celery result
backend. Everything in this module is the bookkeeping that connects those
two moments - it holds no model-calling logic itself.

Two pieces of state, both in the cache (Valkey, the same result backend
Celery already uses):

- The per-profile single-flight lock (:func:`acquire_turn_lock`/
  :func:`release_turn_lock`), a thin wrapper over
  ``services.core.locks.acquire_lock``/``release_lock`` - so a user's
  second message while the first is still running gets the pending bubble
  back rather than starting a competing turn, and the task itself can
  detect a stale redelivery by checking whether its lock token still owns
  the lock before it writes a result.
- The turn record (:func:`store_turn_record`/:func:`read_turn_record`) -
  which task id and lock token a turn id maps to, so the poll endpoint can
  look up progress via ``services.core.celery.get_task_progress`` without
  trusting anything the client sends beyond the id itself, and can refuse a
  poll from a profile that isn't the one who started the turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from django.core.cache import cache

from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Growing poll schedule (seconds), shared by the web partial's
#: ``hx-trigger="load delay:{{ }}s"`` and the external API's
#: ``poll_after_seconds`` - fast at first (a short turn should feel near-
#: instant), backing off so a slow turn doesn't hammer the poll endpoint.
#: See :func:`turn_poll_delay`.
TURN_POLL_INTERVAL_SECONDS: tuple[int, ...] = (1, 1, 2, 2, 3, 3, 5, 5)
#: A poller gives up after this many attempts. Matches the schedule's own
#: shape - by the time a client has polled this many times (the schedule's
#: tail is 5s, so this is several minutes), the turn task's own
#: ``time_limit`` (120s) has long since resolved it one way or another, so a
#: poller still going has lost the result, not the task.
MAX_POLL_ATTEMPTS = 60

#: Turn record cache TTL - long enough that a client which briefly lost
#: connectivity can still resume polling, short enough that an abandoned
#: turn's small cache entry doesn't linger indefinitely.
_TURN_RECORD_TTL_SECONDS = 15 * 60
#: Lock TTL: must exceed the turn task's own hard ``time_limit`` (120s) so
#: the lock never expires while the task it is guarding is still
#: legitimately running - see run_assistant_turn_task's own docstring.
_TURN_LOCK_TTL_SECONDS = 180


def turn_poll_delay(attempt: int) -> int:
    """The delay (seconds) before the ``attempt``-th poll.

    Args:
        attempt: 0-indexed poll attempt number. Negative values are treated
            as 0 rather than raising - a caller-supplied attempt count is
            just a pacing hint, not worth failing a poll over.

    Returns:
        :data:`TURN_POLL_INTERVAL_SECONDS`'s value at that index, or its
        last value once the schedule is exhausted - later polls keep
        happening at the slowest configured cadence rather than stopping.
    """
    index = min(max(attempt, 0), len(TURN_POLL_INTERVAL_SECONDS) - 1)
    return TURN_POLL_INTERVAL_SECONDS[index]


def _turn_lock_key(profile: Profile) -> str:
    return f"assistant:turn:{profile.pk}"


def acquire_turn_lock(profile: Profile) -> str | None:
    """Take the per-profile single-flight lock for starting a new turn.

    Args:
        profile: The requesting profile.

    Returns:
        An opaque token to hand to :func:`release_turn_lock`, or ``None`` if
        this profile already has a turn in flight - the caller must return
        the existing pending bubble rather than enqueue a second turn.
    """
    return acquire_lock(_turn_lock_key(profile), _TURN_LOCK_TTL_SECONDS)


def release_turn_lock(profile: Profile, token: str | None) -> None:
    """Release the lock :func:`acquire_turn_lock` took, if it is still ours.

    Args:
        profile: The same profile passed to :func:`acquire_turn_lock`.
        token: The token it returned. ``None`` is a no-op.
    """
    release_lock(_turn_lock_key(profile), token)


def turn_lock_is_current(profile: Profile, token: str) -> bool:
    """Whether ``token`` is still the current holder of the per-profile turn lock.

    The turn task calls this before doing any real work: if the lock has
    already expired and been re-acquired by a newer turn (the TTL lapsed
    while this task sat queued, or a prior run of it never released -
    ``acks_late=False`` means that should not redeliver, but this check
    does not depend on that guarantee holding), a stale task must not spend
    a provider call on a turn nothing is polling for anymore, or - if it
    ran to completion regardless - release a lock a newer turn now owns.
    :func:`release_turn_lock` is separately self-guarding for that second
    part (see :func:`~services.core.locks.release_lock`); this check is
    what avoids doing the work at all.

    Args:
        profile: The profile whose lock to check.
        token: The token this caller was given by :func:`acquire_turn_lock`.

    Returns:
        True if ``token`` is still the current holder.
    """
    return cache.get(_turn_lock_key(profile)) == token


def _turn_record_key(turn_id: str) -> str:
    return f"ulai:turn:{turn_id}"


def new_turn_id() -> str:
    """A fresh, unguessable turn id for the client to poll."""
    return uuid4().hex


def store_turn_record(turn_id: str, *, profile_id: int, task_id: str, lock_token: str) -> None:
    """Record which task and lock a turn id maps to.

    Args:
        turn_id: The turn id returned to the client.
        profile_id: The requesting profile's id. The poll endpoint checks
            this against the polling request's own profile before returning
            anything - a turn id never answers a different profile's poll.
        task_id: The Celery task id ``services.core.celery.get_task_progress``
            reads.
        lock_token: The single-flight lock token this turn holds, so
            ``run_assistant_turn_task`` can verify it still owns the lock
            before writing a result - a stale redelivery of an already-
            resolved (or superseded) turn must not clobber a newer one.
    """
    cache.set(_turn_record_key(turn_id), {"profile_id": profile_id, "task_id": task_id, "lock_token": lock_token}, _TURN_RECORD_TTL_SECONDS)


def read_turn_record(turn_id: str) -> dict[str, Any] | None:
    """The record :func:`store_turn_record` wrote for ``turn_id``.

    Returns:
        The stored ``{"profile_id", "task_id", "lock_token"}`` mapping, or
        ``None`` if it expired or a turn id this cache never issued was polled.
    """
    record = cache.get(_turn_record_key(turn_id))
    return record if isinstance(record, dict) else None
