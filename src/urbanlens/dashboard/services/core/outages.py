"""Whether any upstream call in a block went unanswered, for code whose gateways turn failures into "nothing found".

A caller about to store a settled "nothing here" asks this first: an answer built while a source couldn't be reached
is not one to keep.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Status codes that mean the upstream couldn't answer for now.
_UNANSWERED_STATUSES = frozenset({408, 429})


@dataclass(slots=True)
class OutageLog:
    """The services that couldn't be reached inside one :func:`outages_observed` block.

    Attributes:
        services: Each unanswered call's service key, in order.
    """

    services: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.services)


_ACTIVE: ContextVar[tuple[OutageLog, ...]] = ContextVar("upstream_outage_logs", default=())


@contextmanager
def outages_observed() -> Iterator[OutageLog]:
    """Record every upstream call inside the block that went unanswered; nested blocks each see their own calls.

    Yields:
        The log, filled as calls fail.
    """
    log = OutageLog()
    token = _ACTIVE.set((*_ACTIVE.get(), log))
    try:
        yield log
    finally:
        _ACTIVE.reset(token)


def record_unanswered(service_key: str) -> None:
    """Note that a call to *service_key* got no answer: it failed to connect, timed out, was throttled or got a 5xx.

    Args:
        service_key: The rate limiter's key for the service.
    """
    for log in _ACTIVE.get():
        log.services.append(service_key)


def is_unanswered_status(status_code: int) -> bool:
    """Whether an HTTP status means the upstream couldn't answer for now.

    Args:
        status_code: The response's status.

    Returns:
        True for a 5xx, 408 or 429.
    """
    return status_code >= 500 or status_code in _UNANSWERED_STATUSES
