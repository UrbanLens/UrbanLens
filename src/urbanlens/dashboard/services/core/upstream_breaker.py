"""Stop calling an upstream that has told this deployment to wait.

A throttled REData refuses every call until its window reopens, so each call made in the meantime
is a refusal that also keeps the key's window full. A tripped breaker stores the retry-at time in the
shared cache, and every process short-circuits the calls it guards until then, without a request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import hashlib
import logging
import re
import time
from typing import TYPE_CHECKING, ClassVar, NamedTuple
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
        self.announce(scope, seconds, retry_at)

    def announce(self, scope: str, seconds: int, retry_at: float) -> None:
        """Report that ``scope`` was opened.

        Args:
            scope: The scope opened.
            seconds: How long it is held open.
            retry_at: When it closes, as a Unix time.
        """
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


def _named_providers(url: str, params: object) -> set[str]:
    """The provider tags a request named, from its URL and from what was passed to ``requests``, one or a list."""
    named = {value for key, value in parse_qsl(urlsplit(url).query) if key == "provider"}
    value = params.get("provider") if isinstance(params, Mapping) else None
    if isinstance(value, str):
        named.add(value)
    elif isinstance(value, list | tuple):
        named.update(str(tag) for tag in value)
    return {tag for tag in named if tag}


def _api_path(url: str) -> str:
    """The path after ``/api/v1/``, without a leading slash."""
    path = urlsplit(url).path
    marker = "/api/v1/"
    return path.split(marker, 1)[1] if marker in path else path.lstrip("/")


class RefusedEndpoint(NamedTuple):
    """An endpoint REData refused this deployment's key, as the site admin sees it.

    Attributes:
        endpoint: The endpoint, its identifiers blanked (``parcels/{id}/owners``).
        refused_at: When REData last refused it, as a Unix time.
        retry_at: When it will next be asked, as a Unix time.
    """

    endpoint: str
    refused_at: float
    retry_at: float


class RedataBreaker(UpstreamBreaker):
    """REData's per-key throttles and its per-source budgets.

    REData throttles each key in pools: every endpoint but a tile draws on the key's default budget,
    the endpoints that can start a live fetch draw on the smaller lookup budget as well, tiles have a
    pool of their own, and a few writes have theirs. A 429 trips the pool the endpoint draws from.
    A 503 that names a wait, REData's ``rate_limited`` error, or a provider behind the endpoint answering
    REData with a 429 or 5xx is one of REData's own sources out of budget or down: it trips that source
    only, so a Places outage leaves parcels alone.
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
    #: REData's codes for a provider failing behind the endpoint. They also cover one bad input (Google's 400 for a
    #: malformed place id), so only a message naming the provider's 429 or 5xx trips the breaker.
    PROVIDER_FAILURE_ERRORS: ClassVar[frozenset[str]] = frozenset({"places_api_unavailable", "search_unavailable"})
    PROVIDER_REFUSED: ClassVar[re.Pattern[str]] = re.compile(r"\b(?:answered|status)\s+(?:429|5\d\d)\b")
    #: A provider's outcome in REData's envelope when REData could not reach it or had no budget left for it. Read
    #: only for the provider a request named: REData picks a county's provider by the point, so one it chose may not
    #: be the one the next point needs.
    PROVIDER_UNANSWERED: ClassVar[frozenset[str]] = frozenset({"unavailable", "rate_limited"})
    #: None of them names a wait.
    SOURCE_BUSY_SECONDS: ClassVar[int] = 60
    #: REData's answers for a key it will not serve at an endpoint: no key it recognises, or a scope the key lacks.
    REFUSED_STATUSES: ClassVar[frozenset[int]] = frozenset({401, 403})
    #: How long a refused endpoint is left alone. Waiting does not grant a scope - someone editing the key does - so
    #: this is how long such a fix can go unnoticed, traded against one refused call per endpoint per period.
    REFUSED_SECONDS: ClassVar[int] = 3600
    #: How long a refusal is remembered after the last one, so a standing refusal is reported once rather than hourly.
    REFUSAL_MEMORY_SECONDS: ClassVar[int] = 24 * 3600
    #: Scope prefix of a refused endpoint.
    REFUSED_SCOPE: ClassVar[str] = "refused:"

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
    PROVIDER_PARAMS: ClassVar[tuple[str, ...]] = ("provider", "source", "images")

    @staticmethod
    def endpoint(url: str) -> str:
        """The endpoint behind a URL, its identifiers blanked.

        Args:
            url: A REData URL.

        Returns:
            Such as ``hazards`` or ``parcels/{id}/owners``.
        """
        segments = [segment for segment in _api_path(url).split("/") if segment]
        return "/".join("{id}" if any(char.isdigit() for char in segment) else segment for segment in segments) or "root"

    def source(self, url: str, params: object = None) -> str:
        """The providers behind a request: its endpoint with identifiers blanked, and any provider it named.

        Args:
            url: A REData URL.
            params: The request's query parameters.

        Returns:
            Such as ``places/search/nearby`` or ``street-view/timeline?provider=kartaview``.
        """
        endpoint = self.endpoint(url)
        query = _query(url, params)
        chosen = "&".join(f"{name}={query[name]}" for name in self.PROVIDER_PARAMS if query.get(name))
        return f"{endpoint}?{chosen}" if chosen else endpoint

    def scopes(self, url: str, params: object = None) -> tuple[str, ...]:
        """The pool and the source behind ``url``.

        Args:
            url: The URL about to be requested.
            params: The request's query parameters.

        Returns:
            Every pool it draws from, its source, and its endpoint whatever providers were named.
        """
        return (*(f"pool:{pool}" for pool in self.pools(url)), f"source:{self.source(url, params)}", self._refused_scope(url))

    @staticmethod
    def _key_fingerprint() -> str:
        """Which REData key a refusal was for, so a new key is asked at once rather than after the old key's hour."""
        from urbanlens.UrbanLens.settings.app import settings

        return hashlib.sha256(str(settings.redata_api_key or "").encode()).hexdigest()[:12]

    def _refused_scope(self, url: str) -> str:
        """The scope a refusal of ``url`` opens: its endpoint, for the key now configured.

        Args:
            url: A REData URL.

        Returns:
            Such as ``refused:3f2a9c0e1b7d:parcels/{id}/owners``.
        """
        return f"{self.REFUSED_SCOPE}{self._key_fingerprint()}:{self.endpoint(url)}"

    def scope_tripped_by(self, url: str, params: object, response: requests.Response) -> str | None:
        """The pool on a 429, the source on a 503 that says it is busy, the endpoint on a refusal.

        Args:
            url: The URL that was requested.
            params: The request's query parameters.
            response: REData's answer.

        Returns:
            The scope to open, or None.
        """
        if response.status_code in self.REFUSED_STATUSES:
            return self._refused_scope(url)
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
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        refused = error in self.PROVIDER_FAILURE_ERRORS and self.PROVIDER_REFUSED.search(str(body.get("message", ""))) is not None
        busy = error == self.SOURCE_BUSY_ERROR or refused or self._named_provider_unanswered(url, params, body)
        return f"source:{self.source(url, params)}" if busy else None

    def _named_provider_unanswered(self, url: str, params: object, body: dict) -> bool:
        """Whether none of the providers the request named answered."""
        named = _named_providers(url, params)
        outcomes = body.get("providers")
        if not named or not isinstance(outcomes, list):
            return False
        unanswered = {outcome.get("provider") for outcome in outcomes if isinstance(outcome, dict) and outcome.get("status") in self.PROVIDER_UNANSWERED}
        return named <= unanswered

    def default_seconds(self, scope: str) -> int:
        """A minute for a busy source, which never names its wait; :attr:`REFUSED_SECONDS` for a refused endpoint.

        Args:
            scope: The scope being tripped.

        Returns:
            Seconds.
        """
        if scope.startswith(self.REFUSED_SCOPE):
            return self.REFUSED_SECONDS
        return self.SOURCE_BUSY_SECONDS if scope.startswith("source:") else super().default_seconds(scope)

    def _refusals_key(self) -> str:
        return self._key(f"refusals:{self._key_fingerprint()}")

    def announce(self, scope: str, seconds: int, retry_at: float) -> None:
        """Report a refused endpoint once, at error level, and remember it for the site admin; anything else as usual.

        Args:
            scope: The scope opened.
            seconds: How long it is held open.
            retry_at: When it closes, as a Unix time.
        """
        if not scope.startswith(self.REFUSED_SCOPE):
            super().announce(scope, seconds, retry_at)
            return
        endpoint = scope.removeprefix(self.REFUSED_SCOPE).partition(":")[2]
        now = time.time()
        try:
            refusals = cache.get(self._refusals_key())
            # Each rewrite renews the whole list's expiry, so an endpoint refused long ago is dropped here.
            refusals = {name: times for name, times in refusals.items() if self._recent(times, now)} if isinstance(refusals, dict) else {}
            known = endpoint in refusals
            refusals[endpoint] = (now, retry_at)
            cache.set(self._refusals_key(), refusals, timeout=self.REFUSAL_MEMORY_SECONDS)
        except _CACHE_ERRORS:
            known = False
        if known:
            logger.info("REData still refuses this key at %s; asking again in %ss", endpoint, seconds)
        else:
            logger.error("REData refused this key at %s - it may lack the endpoint's scope. Not asking again for %ss.", endpoint, seconds)

    def refused_endpoints(self) -> list[RefusedEndpoint]:
        """The endpoints REData has refused this key in the last :attr:`REFUSAL_MEMORY_SECONDS`, most recent first.

        Returns:
            One entry per endpoint; empty when there are none or the cache cannot say.
        """
        try:
            refusals = cache.get(self._refusals_key())
        except _CACHE_ERRORS:
            return []
        if not isinstance(refusals, dict):
            return []
        now = time.time()
        entries = [RefusedEndpoint(str(endpoint), float(times[0]), float(times[1])) for endpoint, times in refusals.items() if self._recent(times, now)]
        return sorted(entries, key=lambda entry: entry.refused_at, reverse=True)

    def _recent(self, times: object, now: float) -> bool:
        """Whether a registry entry is well-formed and its refusal within :attr:`REFUSAL_MEMORY_SECONDS` of ``now``.

        Args:
            times: The entry's ``(refused_at, retry_at)``.
            now: The current Unix time.

        Returns:
            True when the entry still belongs on the list.
        """
        if not isinstance(times, tuple | list) or len(times) != 2 or not all(isinstance(value, int | float) for value in times):
            return False
        return now - float(times[0]) < self.REFUSAL_MEMORY_SECONDS


class WaybackBreaker(UpstreamBreaker):
    """The Internet Archive's throttles, one per host: archive.org answers availability lookups, and web.archive.org
    takes saves and serves the indexes and snapshots.

    A 429, or a 503 naming a wait, refuses every caller; anything else is about the one page asked for.
    """

    name: ClassVar[str] = "wayback"
    #: How long a refusal naming no wait holds the host off.
    BUSY_SECONDS: ClassVar[int] = 300

    def covers(self, service: str) -> bool:
        """The Wayback Machine's service key.

        Args:
            service: The rate-limiter service key.

        Returns:
            True for the Wayback Machine.
        """
        return service == "wayback_machine"

    def scopes(self, url: str, params: object = None) -> tuple[str, ...]:
        """The host the call goes to.

        Args:
            url: The URL about to be requested.
            params: Unused.

        Returns:
            One scope.
        """
        return ((urlsplit(url).hostname or "").lower(),)

    def scope_tripped_by(self, url: str, params: object, response: requests.Response) -> str | None:
        """The host, when it refused every caller.

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
        """Whether the Archive refused for now rather than failed the one page: a 429, or a 503 naming a wait.

        Args:
            response: The Archive's answer.

        Returns:
            True for a refusal for now.
        """
        return response.status_code == 429 or (response.status_code == 503 and bool(response.headers.get("Retry-After")))

    def default_seconds(self, scope: str) -> int:
        """:attr:`BUSY_SECONDS`.

        Args:
            scope: The scope being tripped.

        Returns:
            Seconds.
        """
        return self.BUSY_SECONDS


#: Every upstream with a breaker.
BREAKERS: tuple[UpstreamBreaker, ...] = (RedataBreaker(), WaybackBreaker())


def breaker_for(service: str) -> UpstreamBreaker | None:
    """The breaker guarding ``service``'s calls.

    Args:
        service: The rate-limiter service key.

    Returns:
        The breaker, or None for an upstream without one.
    """
    return next((breaker for breaker in BREAKERS if breaker.covers(service)), None)
