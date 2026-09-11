"""A per-caller inbound rate limit for plain Django views.
Fixed windows rather than a sliding log: one counter and one TTL per caller per window, which costs two cache operations and cannot grow."""

from __future__ import annotations

from dataclasses import dataclass
import functools
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from django.core.cache import cache
from django.http import HttpResponse

from urbanlens.dashboard.services.security.client_ip import client_ip

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer. `RuntimeError` is the test
#: suite's network guard, and `ValueError` is what `incr` raises when the key
#: expired between the add and the increment.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: Methods that cost something. A GET renders the form; the POST hashes the
#: password, so counting GETs would throttle people reading the page.
COUNTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Rate:
    """How many calls one caller may make in one window."""

    limit: int
    window_seconds: int


#: For the unauthenticated endpoints that hash a password or send mail.
#: Ten in five minutes is far above what signing up or asking for a reset takes and far below what a
#: loop costs: at roughly a second of PBKDF2 each, ten is ten seconds of one worker rather than five
#: minutes of all of them.
ANONYMOUS_EXPENSIVE = Rate(limit=10, window_seconds=300)


def _window_start(rate: Rate, now: float | None = None) -> int:
    """The current window's index, so a key expires with its window."""
    return int((now if now is not None else time.time()) // rate.window_seconds)


def _key(scope: str, identity: str, rate: Rate) -> str:
    return f"ul:throttle:{scope}:{_window_start(rate)}:{identity}"


def _cache_add(key: str, timeout: int) -> bool:
    """Seeded separately so a test can make the cache unreachable.

    Args:
        key: The counter key.
        timeout: Seconds until it expires.

    Returns:
        Whether this call created the counter.
    """
    return bool(cache.add(key, 1, timeout=timeout))


def allow(scope: str, identity: str, rate: Rate) -> bool:
    """Whether *identity* may make another call in *scope* right now.

    Args:
        scope: What is being limited, so two endpoints do not share a budget.
        identity: Who is calling, usually the client IP.
        rate: The limit and its window.

    Returns:
        ``True`` if the call is permitted."""
    key = _key(scope, identity, rate)
    try:
        if _cache_add(key, rate.window_seconds):
            return rate.limit >= 1
        count = cache.incr(key)
    except _CACHE_ERRORS:
        logger.warning("throttle %s could not read its counter; allowing the call", scope, exc_info=True)
        return True
    return bool(count <= rate.limit)


def retry_after(scope: str, identity: str, rate: Rate) -> int:
    """Seconds until *identity*'s window in *scope* resets.

    Args:
        scope: What is being limited.
        identity: Who is calling.
        rate: The limit and its window.

    Returns:
        Whole seconds, never less than one.
    """
    del scope, identity
    elapsed = time.time() % rate.window_seconds
    return max(1, math.ceil(rate.window_seconds - elapsed)) if elapsed else rate.window_seconds


def refusal(scope: str, identity: str, rate: Rate) -> HttpResponse:
    """The response a throttled caller gets.

    Args:
        scope: What is being limited.
        identity: Who is calling.
        rate: The limit and its window.

    Returns:
        A 429 carrying `Retry-After`, which is the only thing that tells a
        well-behaved client to back off rather than retry immediately.
    """
    wait = retry_after(scope, identity, rate)
    response = HttpResponse("Too many requests. Try again shortly.", status=429, content_type="text/plain")
    response.headers["Retry-After"] = str(wait)
    return response


def throttled(scope: str, rate: Rate) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Limit how often one caller may reach the decorated view.

    Args:
        scope: What is being limited.
        rate: The limit and its window.

    Returns:
        The decorator.
    """

    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        # `functools.wraps` rather than copying a name and a docstring: an `as_view()` result
        # carries `view_class`, `view_initkwargs` and any `csrf_exempt` mark in its `__dict__`, and
        # Django and its tooling read them off the callable the URLconf holds.
        @functools.wraps(view)
        def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            if request.method not in COUNTED_METHODS:
                return view(request, *args, **kwargs)
            identity = client_ip(request) or "unknown"
            if not allow(scope, identity, rate):
                logger.warning("throttled %s for %s at %s/%ss", scope, identity, rate.limit, rate.window_seconds)
                return refusal(scope, identity, rate)
            return view(request, *args, **kwargs)

        return guarded

    return decorate
