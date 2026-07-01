"""Base gateway class for external API integrations.

Subclasses declare a ``service_key`` class variable (e.g. ``"nps"``) to opt
into automatic rate limiting and call logging via ``_RateLimitedSession``.
When ``service_key`` is set the plain ``requests.Session`` is replaced in
``__post_init__`` with a wrapper that checks ``ApiRateLimit`` config before
every request and writes an ``ApiCallLog`` row after.

Subclasses that override ``__post_init__`` **must** call
``Gateway.__post_init__(self)`` so the session swap takes effect.
Do not use zero-argument ``super()`` — it fails in ``slots=True`` dataclasses
when the ``__class__`` cell references the pre-slots class object.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import ClassVar

import requests


@dataclass(slots=True, kw_only=True)
class Gateway(ABC):  # noqa: B024 - Abstract so it cannot be instantiated directly
    """An abstract class to serve as a template for API gateways.

    Class variables (set on subclasses, not dataclass fields):
        service_key: Unique identifier for this service (e.g. ``"nps"``).
            Set this to enable automatic rate limiting and call logging.
            Leave as ``None`` to opt out (no limiting, no logging).

    Attributes:
        session: The HTTP session used for all requests. When ``service_key``
            is set this is replaced with a ``_RateLimitedSession`` in
            ``__post_init__``.
    """

    service_key: ClassVar[str | None] = None

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
