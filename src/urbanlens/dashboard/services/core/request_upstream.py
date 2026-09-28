"""Calling an upstream from a request thread: cache, then throttle, then a slot, then a deadline.

Each upstream a view calls is one :class:`RequestUpstream` subclass. It is an :class:`UpstreamSlots`, so
it has its own per-process count, and it names the deadline and per-caller rate that apply to it.

The slot is held by the thread doing the fetch, not by the request waiting on it. A request that gives
up at the deadline leaves its fetch running (Python cannot stop a blocked thread), and that fetch keeps
its slot until it returns. So a slow upstream fills its own slots and later callers are told it is busy
at once, rather than piling abandoned calls into the deadline pool every other caller shares.

A fetch that finishes after its caller gave up still caches what it got, so the next request is a hit.
Only a value the fetch returned is cached; a raised error never is.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError, wait
from dataclasses import dataclass
import enum
import hashlib
import logging
import pickle
from typing import TYPE_CHECKING, Any, ClassVar

from django.http import JsonResponse
import requests

from urbanlens.dashboard.services.core import bounded_cache
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, UpstreamBusyError
from urbanlens.dashboard.services.core.timeout_utils import submit_bounded
from urbanlens.dashboard.services.core.upstream_slots import UpstreamSlots
from urbanlens.dashboard.services.security import throttle
from urbanlens.dashboard.services.security.throttle import Rate
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from concurrent.futures import Future
    import threading

logger = logging.getLogger(__name__)

#: What a gateway raises when the upstream failed rather than the code calling it.
UPSTREAM_ERRORS: tuple[type[Exception], ...] = (GatewayRequestError, requests.RequestException, OSError)


class Outcome(enum.StrEnum):
    """How a call ended."""

    FRESH = "fresh"
    CACHED = "cached"
    THROTTLED = "throttled"
    BUSY = "busy"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


_ANSWERED = frozenset({Outcome.FRESH, Outcome.CACHED})

_REFUSAL_STATUS = {Outcome.THROTTLED: 429, Outcome.BUSY: 503, Outcome.TIMED_OUT: 503, Outcome.FAILED: 502}


@dataclass(frozen=True, slots=True)
class UpstreamResult[T]:
    """What a call produced, and why when it produced nothing.

    Attributes:
        outcome: How the call ended.
        value: The fetched or cached value, when there is one.
        error: The exception the fetch raised, for :attr:`Outcome.FAILED`.
        retry_after: Seconds the caller should wait before asking again, when that is known.
    """

    outcome: Outcome
    value: T | None = None
    error: Exception | None = None
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        """Whether the upstream answered, now or earlier."""
        return self.outcome in _ANSWERED

    @property
    def cached(self) -> bool:
        """Whether the answer came from the cache."""
        return self.outcome is Outcome.CACHED

    @property
    def http_status(self) -> int:
        """The status a view answers with when it has nothing to show: 200 when the upstream answered."""
        if self.outcome is Outcome.FAILED and self.retry_after is not None:
            return 503
        return _REFUSAL_STATUS.get(self.outcome, 200)

    def value_or(self, default: T) -> T:
        """The value when the upstream answered, else *default*.

        Args:
            default: What to use when there is no answer.

        Returns:
            The value or *default*.
        """
        return self.value if self.ok and self.value is not None else default


@dataclass(slots=True)
class Pending[T]:
    """A call that has been started, or answered without starting.

    Attributes:
        upstream: The policy the call runs under.
        resolved: The result, once known.
        future: The running fetch, when one was started.
        semaphore: The semaphore the fetch holds a slot of.
        errors: Exception types that mean the upstream failed.
    """

    upstream: type[RequestUpstream]
    resolved: UpstreamResult[T] | None = None
    future: Future[T] | None = None
    semaphore: threading.BoundedSemaphore | None = None
    errors: tuple[type[Exception], ...] = UPSTREAM_ERRORS

    def result(self, timeout: float | None = None) -> UpstreamResult[T]:
        """Wait for the answer, up to *timeout* seconds.

        Args:
            timeout: Seconds to wait; defaults to the upstream's deadline. After :func:`wait_all`, pass 0.

        Returns:
            The result. Asking again returns the same one.

        Raises:
            Exception: Whatever the fetch raised that is not one of :attr:`errors` - a bug, not an outage.
        """
        if self.resolved is not None:
            return self.resolved
        future = self.future
        if future is None:
            raise RuntimeError("A pending call has neither a result nor a future.")
        name = self.upstream.name
        budget = self.upstream.deadline if timeout is None else timeout
        try:
            value = future.result(timeout=budget)
        except FutureTimeoutError:
            if future.cancel():
                if self.semaphore is not None:
                    self.semaphore.release()
                logger.warning("Upstream %s never started before its deadline; the deadline pool is saturated", name)
            else:
                logger.warning("Upstream %s missed its %.0fs deadline; leaving the fetch to finish in the background", name, self.upstream.deadline)
            self.resolved = UpstreamResult(Outcome.TIMED_OUT, retry_after=self.upstream.busy_retry_seconds)
        except self.errors as exc:
            # Only the type: a requests error's text is the full URL, key and coordinates included.
            logger.warning("Upstream %s failed: %s", name, type(exc).__name__)
            retry_after = exc.retry_after if isinstance(exc, UpstreamBusyError) else None
            self.resolved = UpstreamResult(Outcome.FAILED, error=exc, retry_after=retry_after)
        else:
            self.resolved = UpstreamResult(Outcome.FRESH, value=value)
        return self.resolved


class RequestUpstream(UpstreamSlots):
    """The policy for one upstream called on a request thread.

    Subclass it and set :attr:`name`; override :attr:`deadline`, :attr:`rate` or :meth:`limit` where the
    upstream needs them. Each subclass gets its own semaphore, so one slow upstream cannot take another's
    slots.
    """

    #: Names the upstream in logs, cache keys and the throttle scope.
    name: ClassVar[str]
    #: Seconds a request waits for the fetch.
    deadline: ClassVar[float] = 10.0
    #: How many uncached calls one caller may make.
    rate: ClassVar[Rate] = Rate(limit=60, window_seconds=60)
    #: What a request that found every slot taken is told to wait.
    busy_retry_seconds: ClassVar[int] = 2
    #: Exception types that mean the upstream failed.
    errors: ClassVar[tuple[type[Exception], ...]] = UPSTREAM_ERRORS

    @classmethod
    def limit(cls) -> int:
        """How many fetches from this upstream one process may have in flight.

        Returns:
            ``request_upstream_concurrency``.
        """
        return app_settings.request_upstream_concurrency

    @classmethod
    def throttle_scope(cls) -> str:
        """The throttle counter this upstream charges.

        Returns:
            The scope.
        """
        return f"upstream.{cls.name}"

    @classmethod
    def cache_key(cls, key: str) -> str:
        """The cache key for one answer, hashed so any caller-supplied text makes a valid key.

        Args:
            key: What identifies the answer, e.g. a normalised query.

        Returns:
            The key.
        """
        digest = hashlib.sha256(key.encode()).hexdigest()[:40]
        return f"ul:upstream:{cls.name}:{digest}"

    @classmethod
    def cached(cls, key: str) -> Any | None:
        """A stored answer, or None when there is none or the cache cannot answer.

        Args:
            key: What identifies the answer.

        Returns:
            The value.
        """
        return bounded_cache.get_or_none(cls.cache_key(key), label=f"{cls.name} answer")

    @classmethod
    def store(cls, key: str, value: object, ttl: int) -> bool:
        """Store an answer, tolerating a cache that cannot take it.

        Args:
            key: What identifies the answer.
            value: The answer.
            ttl: Seconds to keep it.

        Returns:
            Whether it was stored.
        """
        try:
            return bounded_cache.set_or_skip(cls.cache_key(key), value, ttl, label=f"{cls.name} answer")
        except (pickle.PicklingError, TypeError, AttributeError):
            logger.warning("Upstream %s returned an answer the cache cannot hold", cls.name, exc_info=True)
            return False

    @classmethod
    def start[T](
        cls,
        fetch: Callable[[], T],
        *,
        key: str | None = None,
        ttl: int = 0,
        caller: str | None = None,
        cacheable: Callable[[T], bool] | None = None,
        errors: tuple[type[Exception], ...] = (),
    ) -> Pending[T]:
        """Answer from the cache, or start the fetch without waiting for it.

        Args:
            fetch: Zero-argument callable that asks the upstream.
            key: What identifies the answer; with *ttl*, the answer is cached under it.
            ttl: Seconds to cache the answer; 0 caches nothing.
            caller: Who to charge, usually :func:`~urbanlens.dashboard.services.security.throttle.account_or_address`.
                None charges nobody, for a route throttled elsewhere.
            cacheable: Whether a returned value may be cached. Defaults to anything but None.
            errors: More exception types that mean the upstream failed, beyond :attr:`errors`.

        Returns:
            The pending call.
        """
        if key is not None and ttl > 0:
            hit = cls.cached(key)
            if hit is not None:
                return Pending(cls, resolved=UpstreamResult(Outcome.CACHED, value=hit))

        if caller is not None:
            scope = cls.throttle_scope()
            if not throttle.allow(scope, caller, cls.rate):
                logger.warning("throttled %s for %s at %s/%ss", scope, caller, cls.rate.limit, cls.rate.window_seconds)
                return Pending(cls, resolved=UpstreamResult(Outcome.THROTTLED, retry_after=throttle.retry_after(scope, caller, cls.rate)))

        semaphore = cls.semaphore()
        if not semaphore.acquire(blocking=False):
            return Pending(cls, resolved=UpstreamResult(Outcome.BUSY, retry_after=cls.busy_retry_seconds))

        def work() -> T:
            try:
                value = fetch()
            finally:
                semaphore.release()
            if key is not None and ttl > 0 and (cacheable(value) if cacheable is not None else value is not None):
                cls.store(key, value, ttl)
            return value

        try:
            future = submit_bounded(work)
        except RuntimeError:
            semaphore.release()
            raise
        return Pending(cls, future=future, semaphore=semaphore, errors=cls.errors + errors)

    @classmethod
    def call[T](
        cls,
        fetch: Callable[[], T],
        *,
        key: str | None = None,
        ttl: int = 0,
        caller: str | None = None,
        cacheable: Callable[[T], bool] | None = None,
        errors: tuple[type[Exception], ...] = (),
    ) -> UpstreamResult[T]:
        """:meth:`start` the call and wait up to :attr:`deadline` for it.

        Args:
            fetch: Zero-argument callable that asks the upstream.
            key: What identifies the answer.
            ttl: Seconds to cache the answer; 0 caches nothing.
            caller: Who to charge, or None.
            cacheable: Whether a returned value may be cached.
            errors: More exception types that mean the upstream failed.

        Returns:
            The result.
        """
        return cls.start(fetch, key=key, ttl=ttl, caller=caller, cacheable=cacheable, errors=errors).result()


def wait_all(pendings: Iterable[Pending[Any]], *, timeout: float) -> None:
    """Wait for several started calls under one budget; read each with ``result(0)`` afterwards.

    Args:
        pendings: The calls.
        timeout: Seconds to wait for all of them together.
    """
    futures = [pending.future for pending in pendings if pending.resolved is None and pending.future is not None]
    if futures:
        wait(futures, timeout=timeout)


def refusal_json(result: UpstreamResult[Any], payload: dict[str, Any]) -> JsonResponse:
    """A JSON answer for a call that produced nothing, with the status and wait it implies.

    Args:
        result: The unanswered result.
        payload: The body, usually the endpoint's empty shape.

    Returns:
        The response, carrying ``Retry-After`` when a wait is known.
    """
    response = JsonResponse({**payload, "unavailable": result.outcome.value}, status=result.http_status)
    if result.retry_after is not None:
        response["Retry-After"] = str(result.retry_after)
    return response
