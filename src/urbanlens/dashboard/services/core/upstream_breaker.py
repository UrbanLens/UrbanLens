"""Stop calling an upstream that has told this deployment to wait.

A throttled REData refuses every call until its window reopens, so each call made in the meantime
is a refusal that also keeps the key's window full. A tripped breaker stores the retry-at time in the
shared cache, and every process short-circuits the calls it guards until then, without a request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import logging
import re
import time
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qsl, urlsplit

from django.core.cache import cache

from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_DEFAULT_SECONDS, upstream_retry_after

if TYPE_CHECKING:
    from collections.abc import Iterable

    import requests

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer; `RuntimeError` is the test suite's network guard.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: Longest wait a breaker honours, so a malformed wait cannot silence an upstream for a day.
BREAKER_MAX_SECONDS = 3600


class UpstreamBreaker(ABC):
    """The breakers guarding one upstream's calls, and the responses that trip them.

    A call is guarded by several scopes at once - the key's shared budget and the one source behind
    the endpoint - and is short-circuited while any of them is open.
    """

    #: Namespace for this upstream's cache keys.
    name: ClassVar[str]

    @abstractmethod
    def covers(self, service: str) -> bool:
        """Whether this upstream owns ``service``'s calls.

        Args:
            service: The rate-limiter service key.
        """

    @abstractmethod
    def scopes(self, url: str, params: object = None) -> tuple[str, ...]:
        """The breakers a call to ``url`` must find closed.

        Args:
            url: The URL about to be requested.
            params: The request's query parameters, as handed to ``requests``.
        """

    @abstractmethod
    def scope_tripped_by(self, url: str, params: object, response: requests.Response) -> str | None:
        """Which breaker ``response`` trips, if any.

        Args:
            url: The URL that was requested.
            params: The request's query parameters.
            response: The upstream's answer.
        """

    def _key(self, scope: str) -> str:
        return f"upstream-breaker:{self.name}:{scope}"

    def wait(self, url: str, params: object = None) -> int | None:
        """Seconds until every breaker guarding ``url`` is closed.

        Args:
            url: The URL about to be requested.
            params: The request's query parameters.

        Returns:
            The wait, or None when the call may go out. An unreadable cache lets the call go out.
        """
        keys = [self._key(scope) for scope in self.scopes(url, params)]
        try:
            retry_at = [value for value in cache.get_many(keys).values() if isinstance(value, int | float)]
        except _CACHE_ERRORS:
            return None
        remaining = max(retry_at, default=0.0) - time.time()
        return max(1, int(remaining + 0.999)) if remaining > 0 else None

    def observe(self, url: str, params: object, response: requests.Response) -> None:
        """Trip the breaker ``response`` names, for as long as the upstream asked.

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
        """How long to open ``scope`` when the response named no wait.

        Args:
            scope: The scope being tripped.

        Returns:
            Seconds.
        """
        return UPSTREAM_BUSY_DEFAULT_SECONDS

    def trip(self, scope: str, seconds: int) -> None:
        """Open ``scope`` for ``seconds``, never shortening a longer wait already recorded.

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
        except _CACHE_ERRORS:
            logger.warning("Could not record the %s breaker for %s", self.name, scope, exc_info=True)
            return
        logger.warning("%s breaker %s open for %ss", self.name, scope, seconds)


def _query(url: str, params: object) -> dict[str, str]:
    """The query parameters of a request, from its URL and from what was passed to ``requests``."""
    query = dict(parse_qsl(urlsplit(url).query))
    if isinstance(params, Mapping):
        pairs: Iterable[object] = params.items()
    elif isinstance(params, list | tuple):
        pairs = params
    else:
        pairs = ()
    for pair in pairs:
        if isinstance(pair, tuple) and len(pair) == 2:
            query[str(pair[0])] = str(pair[1])
    return query


def _api_path(url: str) -> str:
    """The path after ``/api/v1/``, without a leading slash."""
    path = urlsplit(url).path
    marker = "/api/v1/"
    return path.split(marker, 1)[1] if marker in path else path.lstrip("/")


class RedataBreaker(UpstreamBreaker):
    """REData's per-key throttles and its per-source budgets.

    REData throttles each key in pools: every endpoint but a tile draws on the key's default budget,
    the endpoints that can start a live fetch draw on the smaller lookup budget as well, tiles have a
    pool of their own, and a few writes have theirs. A 429 trips the pool the endpoint draws from.
    A 503 that names a wait, or REData's ``rate_limited`` error, is one of REData's own sources out of
    budget or down: it trips that source only, so a Places outage leaves parcels alone.
    """

    name: ClassVar[str] = "redata"

    #: The endpoints REData throttles with ``ApiKeyLookupThrottle`` (``api/views*.py`` on its main branch).
    LOOKUP: ClassVar[re.Pattern[str]] = re.compile(
        r"""^(?:
            parcels/(?:lookup|[^/]+/(?:assessments|sale-records|demographics|national-parks))/
          | places/(?!cid/|resolve-cids/|confirm-coordinates/)
          | (?:points-of-interest|media)/lookup/
          | cultural-resources/(?:lookup/|fetch-details/|[^/]+/(?:fetch-detail/|attachments/\d+/(?:download|extract)/))
          | parks/(?!nearby/)
          | maps/sheets/[^/]+/annotation/
          | (?:geocode|elevation|weather|imagery|street-view|hazards|incidents|underground|power-lines|historical-features
              |buildings|roads|addresses|air-quality|land-cover|walkability|permits|soil|hydrology|nature-observations
              |reference-documents|routes|isochrones|travel-matrix|search)/
        )""",
        re.VERBOSE,
    )
    #: Endpoints with a pool of their own on top of the default budget.
    OWN_POOLS: ClassVar[dict[str, re.Pattern[str]]] = {
        "resolve_cids": re.compile(r"^places/resolve-cids/"),
        "writes": re.compile(r"^(?:labels/(?:assignments/)?$|photos/(?:votes/)?$|places/confirm-coordinates/|floorplans/)"),
    }
    #: Tiles replace the default budget rather than stacking on it.
    TILES: ClassVar[re.Pattern[str]] = re.compile(r"^tiles/(?!sources/)")

    #: REData's code for "every provider behind this endpoint is out of outbound budget". Unlike a
    #: per-county ``source_rate_limited``, it does not depend on the point asked about.
    SOURCE_BUSY_ERROR: ClassVar[str] = "rate_limited"
    #: REData's ``rate_limited`` names no wait.
    SOURCE_BUSY_SECONDS: ClassVar[int] = 60

    def covers(self, service: str) -> bool:
        """REData's services are the ones named for it.

        Args:
            service: The rate-limiter service key.

        Returns:
            True for a REData service.
        """
        return service.startswith("redata")

    def pools(self, url: str) -> tuple[str, ...]:
        """The throttle pools ``url`` draws from, the one a 429 most likely came from first.

        Args:
            url: A REData URL.

        Returns:
            Pool names.
        """
        path = _api_path(url)
        if self.TILES.match(path):
            return ("tiles",)
        if self.LOOKUP.match(path):
            return ("lookup", "default")
        for pool, pattern in self.OWN_POOLS.items():
            if pattern.match(path):
                return (pool, "default")
        return ("default",)

    #: Query parameters that choose which of an endpoint's providers answer.
    PROVIDER_PARAMS: ClassVar[tuple[str, ...]] = ("provider", "source")

    def source(self, url: str, params: object = None) -> str:
        """The providers behind a request: its endpoint with identifiers blanked, and any provider it named.

        Args:
            url: A REData URL.
            params: The request's query parameters.

        Returns:
            Such as ``places/search/nearby`` or ``street-view/timeline?provider=kartaview``.
        """
        segments = [segment for segment in _api_path(url).split("/") if segment]
        endpoint = "/".join("{id}" if any(char.isdigit() for char in segment) else segment for segment in segments) or "root"
        query = _query(url, params)
        chosen = "&".join(f"{name}={query[name]}" for name in self.PROVIDER_PARAMS if query.get(name))
        return f"{endpoint}?{chosen}" if chosen else endpoint

    def scopes(self, url: str, params: object = None) -> tuple[str, ...]:
        """The pool and the source behind ``url``.

        Args:
            url: The URL about to be requested.
            params: The request's query parameters.

        Returns:
            Every pool it draws from, and its source.
        """
        return (*(f"pool:{pool}" for pool in self.pools(url)), f"source:{self.source(url, params)}")

    def scope_tripped_by(self, url: str, params: object, response: requests.Response) -> str | None:
        """The pool on a 429, the source on a 503 that says it is busy.

        Args:
            url: The URL that was requested.
            params: The request's query parameters.
            response: REData's answer.

        Returns:
            The scope to open, or None.
        """
        if response.status_code == 429:
            return f"pool:{self.pools(url)[0]}"
        if response.status_code != 503:
            return None
        if str(response.headers.get("Retry-After", "")).strip():
            return f"source:{self.source(url, params)}"
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and body.get("error") == self.SOURCE_BUSY_ERROR:
            return f"source:{self.source(url, params)}"
        return None

    def default_seconds(self, scope: str) -> int:
        """A minute for a busy source, which never names its wait.

        Args:
            scope: The scope being tripped.

        Returns:
            Seconds.
        """
        return self.SOURCE_BUSY_SECONDS if scope.startswith("source:") else super().default_seconds(scope)


#: Every upstream with a breaker.
BREAKERS: tuple[UpstreamBreaker, ...] = (RedataBreaker(),)


def breaker_for(service: str) -> UpstreamBreaker | None:
    """The breaker guarding ``service``'s calls.

    Args:
        service: The rate-limiter service key.

    Returns:
        The breaker, or None for an upstream without one.
    """
    return next((breaker for breaker in BREAKERS if breaker.covers(service)), None)
