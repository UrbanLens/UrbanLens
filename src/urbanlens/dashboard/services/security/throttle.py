"""A per-caller inbound rate limit for plain Django views.
Fixed windows rather than a sliding log: one counter per caller per window, counted by ``services.core.counters``."""

from __future__ import annotations

from dataclasses import dataclass
import functools
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from django.http import HttpResponse

from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage
from urbanlens.dashboard.services.security.client_ip import client_ip

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Methods that cost something. A GET renders the form; the POST hashes the
#: password, so counting GETs would throttle people reading the page.
COUNTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Rate:
    """How many calls one caller may make in one window.

    Attributes:
        limit: Calls allowed per window.
        window_seconds: Length of the window.
        on_outage: What happens while the counter store is down. ``LOCAL`` keeps
            the limit per process; ``REFUSE`` refuses every call, for a route that
            spends an upstream's budget rather than our own capacity.
    """

    limit: int
    window_seconds: int
    on_outage: Outage = Outage.LOCAL


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


def allow(scope: str, identity: str, rate: Rate) -> bool:
    """Whether *identity* may make another call in *scope* right now.

    Args:
        scope: What is being limited, so two endpoints do not share a budget.
        identity: Who is calling, usually the client IP.
        rate: The limit and its window.

    Returns:
        ``True`` if the call is permitted."""
    try:
        count = counters.hit(_key(scope, identity, rate), rate.window_seconds, on_outage=rate.on_outage)
    except CounterUnavailableError:
        return False
    return count <= rate.limit


def retry_after(scope: str, identity: str, rate: Rate) -> int:
    """Seconds until *identity*'s window in *scope* resets.

    Args:
        scope: What is being limited.
        identity: Who is calling.
        rate: The limit and its window.

    Returns:
        Whole seconds, never less than one."""
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
        A 429 carrying `Retry-After`, which is the only thing that tells a well-behaved client to back off rather than retry immediately."""
    wait = retry_after(scope, identity, rate)
    response = HttpResponse("Too many requests. Try again shortly.", status=429, content_type="text/plain")
    response.headers["Retry-After"] = str(wait)
    return response


def _address_identity(request: HttpRequest) -> str:
    """The default identity: the caller's address."""
    return client_ip(request)


def account_or_address(request: HttpRequest) -> str:
    """The signed-in account, falling back to the address.

    Args:
        request: The incoming request.

    Returns:
        ``"user:<pk>"`` for an authenticated caller, else the address. Prefixed
        so the two namespaces cannot collide - an address is never ``user:``
        anything, but a budget shared by accident between an account and an
        address would be a budget one could spend on the other's behalf.
    """
    user = getattr(request, "user", None)
    user_id = getattr(user, "pk", None)
    if user_id and getattr(user, "is_authenticated", False):
        return f"user:{user_id}"
    return client_ip(request)


def throttled(scope: str, rate: Rate, methods: frozenset[str] = COUNTED_METHODS, identify: Callable[[HttpRequest], str] | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Limit how often one caller may reach the decorated view.

    Args:
        scope: What is being limited.
        rate: The limit and its window.
        methods: Which HTTP methods to count. Defaults to the unsafe ones,
            because throttling every GET by default would put a limiter in
            front of every page on the site. A view whose *expensive* method is
            GET - a proxy that downloads somebody else's bytes, say - passes its
            own set, at the call site where that is visible.
        identify: Who to charge. Defaults to the address, which is the only
            thing an anonymous caller has. A route that is usually reached by a
            signed-in user should pass :func:`account_or_address` instead: an
            address is a poor identity behind NAT, where one office shares one
            budget, and a poor isolation boundary too, since the requirement is
            that one *account* cannot spend everyone else's.

    Returns:
        The decorator.
    """

    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        # `functools.wraps` rather than copying a name and a docstring: an `as_view()` result
        # carries `view_class`, `view_initkwargs` and any `csrf_exempt` mark in its `__dict__`, and
        # Django and its tooling read them off the callable the URLconf holds.
        @functools.wraps(view)
        def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            if request.method not in methods:
                return view(request, *args, **kwargs)
            identity = (identify or _address_identity)(request) or "unknown"
            if not allow(scope, identity, rate):
                logger.warning("throttled %s for %s at %s/%ss", scope, identity, rate.limit, rate.window_seconds)
                return refusal(scope, identity, rate)
            return view(request, *args, **kwargs)

        # Readable off the URLconf, so a test can ask whether a route is
        # actually guarded rather than inferring it from behaviour - which for a
        # limit of several hundred means several hundred requests, and for a
        # route somebody forgot to wrap means a test that passes.
        guarded.__dict__.update(throttle_scope=scope, throttle_rate=rate, throttle_methods=methods, throttle_identify=identify or _address_identity)
        return guarded

    return decorate
