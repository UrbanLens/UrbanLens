"""
Stop calling an upstream that has told this deployment to wait.

A throttled upstream refuses every call until its window reopens, so each call made in the meantime
is a refusal that also keeps the key's window full. A tripped breaker stores the retry-at time in the
shared cache, and every process short-circuits the calls it guards until then, without a request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import time
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from django.core.cache import cache

from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_DEFAULT_SECONDS, upstream_retry_after

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer; `RuntimeError` is the test suite's network guard.
CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: Longest wait a breaker honours, so a malformed wait cannot silence an upstream for a day.
BREAKER_MAX_SECONDS = 3600


class UpstreamBreaker(ABC):
    """
    The breakers guarding one upstream's calls, and the responses that trip them.

    A call is guarded by several scopes at once - the key's shared budget and the one source behind
    the endpoint - and is short-circuited while any of them is open.
    """

    #: Namespace for this upstream's cache keys.
    name: ClassVar[str]

    @abstractmethod
    def covers(self, service: str) -> bool:
        """
        Whether this upstream owns ``service``'s calls.

        Args:
            service: The rate-limiter service key.
        """

    @abstractmethod
    def scopes(self, url: str, params: object = None) -> tuple[str, ...]:
        """
        The breakers a call to ``url`` must find closed.

        Args:
            url: The URL about to be requested.
            params: The request's query parameters, as handed to ``requests``.
        """

    @abstractmethod
    def scope_tripped_by(self, url: str, params: object, response: requests.Response) -> str | None:
        """
        Which breaker ``response`` trips, if any.

        Args:
            url: The URL that was requested.
            params: The request's query parameters.
            response: The upstream's answer.
        """

    def _key(self, scope: str) -> str:
        return f"upstream-breaker:{self.name}:{scope}"

    def wait(self, url: str, params: object = None) -> int | None:
        """
        Seconds until every breaker guarding ``url`` is closed.

        Args:
            url: The URL about to be requested.
            params: The request's query parameters.

        Returns:
            The wait, or None when the call may go out. An unreadable cache lets the call go out.
        """
        keys = [self._key(scope) for scope in self.scopes(url, params)]
        try:
            retry_at = [value for value in cache.get_many(keys).values() if isinstance(value, int | float)]
        except CACHE_ERRORS:
            return None
        remaining = max(retry_at, default=0.0) - time.time()
        return max(1, int(remaining + 0.999)) if remaining > 0 else None

    def observe(self, url: str, params: object, response: requests.Response) -> None:
        """
        Trip the breaker ``response`` names, for as long as the upstream asked.

        Args:
            url: The URL that was requested.
            params: The request's query parameters.
            response: The upstream's answer.
        """
        scope = self.scope_tripped_by(url, params, response)
        if scope is None:
            return
        default = self.default_seconds(scope)
        self.trip(scope, upstream_retry_after(response, maximum=BREAKER_MAX_SECONDS, default=default) or default)

    def default_seconds(self, scope: str) -> int:
        """
        How long to open ``scope`` when the response named no wait.

        Args:
            scope: The scope being tripped.

        Returns:
            Seconds.
        """
        return UPSTREAM_BUSY_DEFAULT_SECONDS

    def trip(self, scope: str, seconds: int) -> None:
        """
        Open ``scope`` for ``seconds``, never shortening a longer wait already recorded.

        Args:
            scope: The breaker to open.
            seconds: How long to hold it open.
        """
        key = self._key(scope)
        retry_at = time.time() + seconds
        try:
            current = cache.get(key)
            if isinstance(current, int | float) and current >= retry_at:
                return
            cache.set(key, retry_at, timeout=seconds)
        except CACHE_ERRORS:
            logger.warning("Could not record the %s breaker for %s", self.name, scope, exc_info=True)
            return
        self.announce(scope, seconds, retry_at)

    def announce(self, scope: str, seconds: int, retry_at: float) -> None:
        """
        Report that ``scope`` was opened.

        Args:
            scope: The scope opened.
            seconds: How long it is held open.
            retry_at: When it closes, as a Unix time.
        """
        logger.warning("%s breaker %s open for %ss", self.name, scope, seconds)


class WaybackBreaker(UpstreamBreaker):
    """
    The Internet Archive's throttles, one per host: archive.org answers availability lookups, and web.archive.org
    takes saves and serves the indexes and snapshots.

    A 429, or a 503 naming a wait, refuses every caller; anything else is about the one page asked for.
    """

    name: ClassVar[str] = "wayback"
    #: How long a refusal naming no wait holds the host off.
    BUSY_SECONDS: ClassVar[int] = 300

    def covers(self, service: str) -> bool:
        """
        The Wayback Machine's service key.

        Args:
            service: The rate-limiter service key.

        Returns:
            True for the Wayback Machine.
        """
        return service == "wayback_machine"

    def scopes(self, url: str, params: object = None) -> tuple[str, ...]:
        """
        The host the call goes to.

        Args:
            url: The URL about to be requested.
            params: Unused.

        Returns:
            One scope.
        """
        return ((urlsplit(url).hostname or "").lower(),)

    def scope_tripped_by(self, url: str, params: object, response: requests.Response) -> str | None:
        """
        The host, when it refused every caller.

        Args:
            url: The URL that was requested.
            params: Unused.
            response: The Archive's answer.

        Returns:
            The scope to trip, or None.
        """
        return self.scopes(url, params)[0] if self.refuses_every_caller(response) else None

    @staticmethod
    def refuses_every_caller(response: requests.Response) -> bool:
        """
        Whether the Archive refused for now rather than failed the one page: a 429, or a 503 naming a wait.

        Args:
            response: The Archive's answer.

        Returns:
            True for a refusal for now.
        """
        return response.status_code == 429 or (response.status_code == 503 and bool(response.headers.get("Retry-After")))

    def default_seconds(self, scope: str) -> int:
        """
        :attr:`BUSY_SECONDS`.

        Args:
            scope: The scope being tripped.

        Returns:
            Seconds.
        """
        return self.BUSY_SECONDS


#: The breakers core ships; plugins add theirs to ``UPSTREAM_BREAKERS``.
CORE_BREAKERS: tuple[UpstreamBreaker, ...] = (WaybackBreaker(),)


def all_breakers() -> tuple[UpstreamBreaker, ...]:
    """Every upstream with a breaker: core's, then each plugin's."""
    from urbanlens.dashboard.plugins.points import UPSTREAM_BREAKERS

    return (*UPSTREAM_BREAKERS, *CORE_BREAKERS)


def breaker_for(service: str) -> UpstreamBreaker | None:
    """
    The breaker guarding ``service``'s calls.

    Args:
        service: The rate-limiter service key.

    Returns:
        The breaker, or None for an upstream without one.
    """
    return next((breaker for breaker in all_breakers() if breaker.covers(service)), None)
