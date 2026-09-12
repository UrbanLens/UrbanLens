"""Base gateway class for external API integrations.

Subclasses declare a ``service_key`` class variable (e.g. ``"nps"``) to opt
into automatic rate limiting and call logging via ``_RateLimitedSession``.
When ``service_key`` is set the plain ``requests.Session`` is replaced in
``__post_init__`` with a wrapper that checks ``ApiRateLimit`` config before
every request and writes an ``ApiCallLog`` row after.

Subclasses that override ``__post_init__`` **must** call
``Gateway.__post_init__(self)`` so the session swap takes effect.
Do not use zero-argument ``super()`` - it fails in ``slots=True`` dataclasses
when the ``__class__`` cell references the pre-slots class object.
"""

from __future__ import annotations

from abc import ABC, ABCMeta
from dataclasses import dataclass, field
import re
from typing import ClassVar

import requests


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
    """An abstract class to serve as a template for our services.

    Class variables (set on subclasses, not dataclass fields):
        service_key: Unique identifier for this service (e.g. ``"nps"``).
            Must be set to enable automatic rate limiting and call logging.
    """

    paid_service: ClassVar[bool] = False
    service_key: ClassVar[str | None] = None


@dataclass(slots=True, kw_only=True)
class Gateway(Service, ABC):
    """A gateway to an external service.

    Attributes:
        session: The HTTP session used for all requests.
    """

    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        """Replace the plain session with a rate-limited wrapper when applicable.

        A custom session (e.g. a test mock) is preserved as-is; only the
        default ``requests.Session`` instance is swapped for a rate-limited one.
        """
        key = type(self).service_key
        if key and type(self.session) is requests.Session:
            from urbanlens.dashboard.services.core.rate_limiter import _RateLimitedSession

            object.__setattr__(self, "session", _RateLimitedSession(key, self.endpoint_for_log))

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """How this gateway's URLs are described in ``ApiCallLog``.

        Defaults to the URL itself, which is what every point-lookup service
        wants. Override where the URL encodes something not worth keeping - a
        map tile's path *is* a coordinate somebody was looking at, and the log
        exists to track volume and cost per service, which the coordinate does
        not contribute to.

        Args:
            url: The URL about to be requested.

        Returns:
            The string to record as the call's endpoint.
        """
        return url


class GatewayRequestError(RuntimeError):
    """Raised when an external gateway call fails or returns an unusable response.

    Swap this for whatever error base class UrbanLens's other gateways already
    raise, if one exists -- this is a self-contained stand-in.
    """


class GatewayRateLimitedError(GatewayRequestError):
    """Raised when an external gateway reports that its own request budget is exhausted.

    A subclass rather than a sibling of :class:`GatewayRequestError`, so every
    existing ``except GatewayRequestError`` keeps working unchanged. Callers
    that loop over many candidates in one run (e.g. scheduled enrichment) can
    catch this specific type to stop early instead of retrying every
    remaining candidate against a budget that will not refill mid-run.
    """


#: Largest body a gateway will pull into the web worker for one proxied file.
#:
#: Generous against real content - a full-resolution listing photo, a map tile,
#: a scanned attachment - and small against the worker's own memory limit, which
#: is the number that matters: under gevent a worker killed for memory takes
#: every other in-flight request on it down too.
MAX_PROXIED_MEDIA_BYTES = 25 * 1024 * 1024


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
        GatewayRequestError: The body is larger than *max_bytes*, or the
            response was not streamed and so cannot be capped.
    """
    if response.raw is None or getattr(response, "_content_consumed", False):
        raise GatewayRequestError(f"{what} was fetched without stream=True, so its size cannot be bounded")
    # One byte past the ceiling: enough to know it was exceeded, without
    # reading the rest of whatever is on the other end.
    body = response.raw.read(max_bytes + 1, decode_content=True)
    if len(body) > max_bytes:
        raise GatewayRequestError(f"{what} is larger than the {max_bytes // (1024 * 1024)}MB limit for proxied media")
    return body
