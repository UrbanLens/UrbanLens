"""Shared helpers for tests that drive a WebSocket consumer and then assert on what it did."""

from __future__ import annotations

import inspect
import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from channels.testing import WebsocketCommunicator

    Predicate = Callable[[list[dict]], "bool | Awaitable[bool]"]

#: How long to wait for the condition. Generous because a loaded CI runner can starve a consumer for seconds,
#: and a passing run never spends it - the loop leaves the moment the condition holds.
SETTLE_WAIT = 10.0

#: The quiet window after the condition holds, so a trailing frame (a second echo, a late error) lands before
#: the caller asserts rather than leaking into whatever the test does next.
SETTLE_QUIET = 0.3


async def settle(
    comm: WebsocketCommunicator,
    until: Predicate | None = None,
    *,
    wait: float = SETTLE_WAIT,
    quiet: float = SETTLE_QUIET,
) -> list[dict]:
    """
    Wait for a consumer to finish what it was sent, and return the frames it answered with.

    ``send_to`` only enqueues. A fixed window of silence (``receive_nothing(timeout=0.3)``) therefore measures how
    fast the runner is, not whether the consumer is done: starved for 0.3 s it looks finished with its work
    half-done. This waits for a *condition* instead - keep receiving frames until ``until`` holds or ``wait``
    passes - and only then drains for ``quiet`` seconds so trailing frames are collected too.

    To prove a budget *stops* writes, wait for the count the budget allows (and the refusal it should draw) and
    then assert that count is unchanged: the quiet window is the "and nothing more" half, and the condition
    guarantees the consumer got far enough for "nothing more" to mean something.

    If the wait runs out first the frames so far are returned rather than raised, so the caller's own assertion
    fails with its own message. Frames must be JSON.

    Args:
        comm: A connected communicator.
        until: Called with the frames received so far, sync or async, before each wait. ``None`` waits for nothing,
            which leaves only the quiet drain. See ``got_frame``, ``rows_at_least`` and ``all_of``.
        wait: Seconds to wait for ``until``.
        quiet: Seconds of silence that end the final drain.

    Returns:
        Every frame received, in order."""
    frames: list[dict] = []
    deadline = time.monotonic() + wait
    while not await _holds(until, frames) and time.monotonic() < deadline:
        if not await comm.receive_nothing(timeout=0.05):
            frames.append(json.loads(await comm.receive_from()))
    while not await comm.receive_nothing(timeout=quiet):
        frames.append(json.loads(await comm.receive_from()))
    return frames


async def _holds(until: Predicate | None, frames: list[dict]) -> bool:
    if until is None:
        return True
    result: Any = until(frames)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


def got_frame(frame_type: str) -> Predicate:
    """A predicate that holds once a frame whose ``type`` is *frame_type* has arrived."""

    def _predicate(frames: list[dict]) -> bool:
        return any(frame.get("type") == frame_type for frame in frames)

    return _predicate


def rows_at_least(count_rows: Callable[[], Awaitable[int]], expected: int) -> Predicate:
    """A predicate that holds once ``count_rows()`` (an async callable) reports at least *expected* rows."""

    async def _predicate(_frames: list[dict]) -> bool:
        return await count_rows() >= expected

    return _predicate


def all_of(*predicates: Predicate) -> Predicate:
    """A predicate that holds once every one of *predicates* does."""

    async def _predicate(frames: list[dict]) -> bool:
        for predicate in predicates:
            if not await _holds(predicate, frames):
                return False
        return True

    return _predicate
