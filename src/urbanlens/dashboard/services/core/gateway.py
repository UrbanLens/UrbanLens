"""Base gateway class for external API integrations.
Subclasses that override ``__post_init__`` **must** call ``Gateway.__post_init__(self)`` so the session swap takes effect."""

from __future__ import annotations

from abc import ABC, ABCMeta
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy
import re
import threading
from typing import ClassVar

import requests
from urllib3.exceptions import HTTPError as Urllib3HTTPError


def _normalize_service_key(class_name: str) -> str:
    """Convert a class name into a stable snake_case service key."""
    name = re.sub(r"(Service|Gateway)$", "", class_name)
    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.casefold()


class ServiceMeta(ABCMeta):
    """Ensure Service subclasses always have a service_key."""

    # Declared here (not in `Service`) so mypy treats every class this metaclass
    # produces as carrying `service_key` -- the attribute is set below, on the
    # class object itself, which `Service`'s own annotation doesn't cover.
    service_key: str | None

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, object],
        **kwargs: object,
    ) -> ServiceMeta:
        cls = super().__new__(mcls, name, bases, namespace, **kwargs)

        if name != "Service" and not getattr(cls, "service_key", None):
            cls.service_key = _normalize_service_key(name)

        return cls


@dataclass(slots=True, kw_only=True)
class Service(ABC, metaclass=ServiceMeta):
    """An abstract class to serve as a template for our services."""

    paid_service: ClassVar[bool] = False
    service_key: ClassVar[str | None] = None


_pooled = threading.local()


def pooled_session(key: str) -> requests.Session:
    """This thread's long-lived session for *key*, so one call after another reuses its open connections.

    One per thread rather than one per process: ``requests.Session`` is not documented as safe to share
    between threads, and a request thread makes one call at a time anyway. It keeps no cookies, so the
    only thing carried from one call to the next is the connection.

    Args:
        key: The service the session is for.

    Returns:
        The session.
    """
    sessions: dict[str, requests.Session] | None = getattr(_pooled, "sessions", None)
    if sessions is None:
        sessions = _pooled.sessions = {}
    session = sessions.get(key)
    if session is None:
        session = requests.Session()
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        sessions[key] = session
    return session


@dataclass(slots=True, kw_only=True)
class Gateway(Service, ABC):
    """A gateway to an external service.

    Attributes:
        session: The HTTP session used for all requests.
    """

    #: Keep connections to the upstream open between calls, on :func:`pooled_session`, rather than open one per
    #: gateway instance. Only for a gateway that sets nothing on its session - headers, auth, cookies - since the
    #: session outlives the instance.
    reuses_connections: ClassVar[bool] = False

    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        """Replace the plain session with a rate-limited wrapper when applicable.
        A custom session (e.g. a test mock) is preserved as-is; only the default ``requests.Session`` instance is swapped for a rate-limited one."""
        key = type(self).service_key
        if key and type(self.session) is requests.Session:
            from urbanlens.dashboard.services.core.rate_limiter import _RateLimitedSession

            session = pooled_session(key) if type(self).reuses_connections else self.session
            object.__setattr__(self, "session", _RateLimitedSession(key, self.endpoint_for_log, session=session))

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """How this gateway's URLs are described in ``ApiCallLog``.

        Args:
            url: The URL about to be requested.

        Returns:
            The string to record as the call's endpoint.
        """
        return url


class GatewayRequestError(RuntimeError):
    """Raised when an external gateway call fails or returns an unusable response."""

    @property
    def is_outage(self) -> bool:
        """Whether nothing was learned about what was asked, so the failure must not be cached as an empty answer.

        True unless a subclass knows the upstream answered, as REData does when it reports "no record here".
        """
        return True


def is_source_outage(exc: BaseException) -> bool:
    """Whether ``exc`` means the upstream could not be asked or did not answer, rather than that it found nothing.

    A ``LocationCache`` row marks its source fetched for the whole cache term, so a caller that stores an empty
    payload after one of these turns a passing outage into a lasting "nothing here".

    Args:
        exc: What a gateway call raised.

    Returns:
        True for a connection failure, a timeout, a 5xx, a throttle, or a gateway error that reports one.
    """
    if isinstance(exc, GatewayRequestError):
        return exc.is_outage
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is None or status >= 500 or status in (408, 429)
    return isinstance(exc, (requests.RequestException, OSError))


class GatewayRateLimitedError(GatewayRequestError):
    """Raised when an external gateway reports that its own request budget is exhausted.

    A subclass rather than a sibling of :class:`GatewayRequestError`, so every
    existing ``except GatewayRequestError`` keeps working unchanged. Callers
    that loop over many candidates in one run (e.g. scheduled enrichment) can
    catch this specific type to stop early instead of retrying every
    remaining candidate against a budget that will not refill mid-run.
    """


#: The wait passed on for a 429/503 that named none.
UPSTREAM_BUSY_DEFAULT_SECONDS = 30
#: The longest wait passed on to a client; an upstream's quarter-hour throttle is still worth a later retry.
UPSTREAM_BUSY_MAX_SECONDS = 900
#: DRF's throttle message, for an upstream that sends the wait in the body and not in ``Retry-After``.
_WAIT_IN_MESSAGE = re.compile(r"available in (\d+) seconds?")


class UpstreamBusyError(GatewayRequestError):
    """The upstream refused for now (throttled, or its own source unavailable) rather than for good.

    Attributes:
        retry_after: Seconds the upstream asked callers to wait, bounded by :data:`UPSTREAM_BUSY_MAX_SECONDS`.
    """

    def __init__(self, *args: object, retry_after: int = UPSTREAM_BUSY_DEFAULT_SECONDS) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


def upstream_retry_after(response: requests.Response, *, maximum: int = UPSTREAM_BUSY_MAX_SECONDS, default: int = UPSTREAM_BUSY_DEFAULT_SECONDS) -> int | None:
    """How long a busy upstream asked callers to wait.

    Args:
        response: The upstream's response.
        maximum: The longest wait to return.
        default: The wait for a response that names none.

    Returns:
        Seconds from ``Retry-After`` or the throttle message, bounded; None unless the status is 429 or 503.
    """
    if response.status_code not in (429, 503):
        return None
    header = str(response.headers.get("Retry-After", "")).strip()
    if header.isdigit():
        seconds = int(header)
    else:
        match = _WAIT_IN_MESSAGE.search(response.text[:1000])
        seconds = int(match.group(1)) if match else default
    return max(1, min(seconds, maximum))


#: Largest body a gateway will pull into the web worker for one proxied file.
#:
#: Generous against real content - a full-resolution listing photo, a map tile,
#: a scanned attachment - and small against the worker's own memory limit, which
#: is the number that matters: a worker killed for memory takes every other
#: in-flight request on it down too.
MAX_PROXIED_MEDIA_BYTES = 25 * 1024 * 1024


#: What a tile proxy will pass through to a browser under this deployment's own origin.
#:
#: An allow-list, because every interesting case is one a deny-list forgets. ``image/svg+xml`` is
#: an image and can carry script; ``text/html`` served from here is a document, on a route that is
#: ``csp_exempt`` because a policy on a tile is 1.2kB of header that can never apply.
SERVABLE_TILE_TYPES = frozenset({"image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"})


def servable_tile_type(content_type: str | None, *, default: str = "image/png") -> str | None:
    """The type a proxied tile may be served as.

    Args:
        content_type: What the upstream declared, header parameters and all.
        default: What to serve a tile the upstream declared no type for. Assuming an image is the
            long-standing behaviour and is safe alongside ``X-Content-Type-Options: nosniff``.

    Returns:
        The type to serve it as, or None when it is not something this origin should hand a
        browser - which is a fact about the upstream, not about the coordinate.
    """
    declared = (content_type or "").partition(";")[0].strip().lower()
    if not declared:
        return default
    return declared if declared in SERVABLE_TILE_TYPES else None


def read_capped(response: requests.Response, *, max_bytes: int = MAX_PROXIED_MEDIA_BYTES, what: str) -> bytes:
    """Read a proxied body, refusing one larger than *max_bytes*.

    The response must have been requested with ``stream=True``. That is not a
    style preference: without it ``requests`` has already read the whole body
    into memory during ``send()``, so a size check here would be measuring
    memory that is already spent, and ``response.raw.read()`` would return an
    empty body - a refusal that looks exactly like a successful download of
    nothing. Handed such a response this raises instead, so the mistake is loud
    at the call site rather than silent in production.

    Args:
        response: A streamed response whose body has not been read.
        max_bytes: Largest body to accept.
        what: What is being downloaded, for the refusal message.

    Returns:
        The body, at most *max_bytes* long.

    Raises:
        GatewayRequestError: The body is larger than *max_bytes*, the
            response was not streamed and so cannot be capped, or the
            connection failed while the body was being read.
    """
    if response.raw is None or getattr(response, "_content_consumed", False):
        raise GatewayRequestError(f"{what} was fetched without stream=True, so its size cannot be bounded")
    # One byte past the ceiling: enough to know it was exceeded, without
    # reading the rest of whatever is on the other end.
    try:
        body = response.raw.read(max_bytes + 1, decode_content=True)
    except Urllib3HTTPError as exc:
        # ``raw.read`` skips the wrapper that turns these into ``requests`` exceptions, so a dropped connection or a read timeout would otherwise escape callers that catch ``GatewayRequestError``.
        raise GatewayRequestError(f"{what} could not be read: {type(exc).__name__}") from exc
    if len(body) > max_bytes:
        raise GatewayRequestError(f"{what} is larger than the {max_bytes // (1024 * 1024)}MB limit for proxied media")
    return body
