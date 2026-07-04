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
            from urbanlens.dashboard.services.rate_limiter import _RateLimitedSession

            object.__setattr__(self, "session", _RateLimitedSession(key))


class GatewayRequestError(RuntimeError):
    """Raised when an external gateway call fails or returns an unusable response.

    Swap this for whatever error base class UrbanLens's other gateways already
    raise, if one exists -- this is a self-contained stand-in.
    """
