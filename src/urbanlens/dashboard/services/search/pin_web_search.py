"""The Private Pin page's web search: which queries a pin makes, and the cached results it may read.

A pin reads the search every viewer of its Location shares, built from the place's public names, and, when it has
names of its own, the search cached for exactly that set (see ``services.pins.search_names``).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from urbanlens.dashboard.services.pins.search_names import search_names

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.search_names import SearchScope

#: The ``LocationCache.source`` web-search results are cached under.
WEB_SEARCH_SOURCE = "web_search"


@dataclass(frozen=True, slots=True)
class WebSearch:
    """One web search a pin's page makes.

    Attributes:
        scope: Who may read its results, and the names it is built from.
        query: The query sent.
    """

    scope: SearchScope
    query: str

    @property
    def query_key(self) -> str:
        """The query as ``LocationCache.query_key`` can hold it."""
        return self.query[:255]


def pin_web_searches(pin: Pin) -> list[WebSearch]:
    """Every web search ``pin``'s page reads, the shared one first; empty when the pin has no name to search for.

    Args:
        pin: The pin whose page is being rendered.

    Returns:
        The searches with a query to send.
    """
    searches = []
    for scope in search_names(pin).scopes:
        if query := pin.get_unique_search_name(scope, quote_name=True, quote_locality=True):
            searches.append(WebSearch(scope, query))
    return searches


def cached_web_searches(location: Location, searches: Sequence[WebSearch]) -> dict[str, LocationCache]:
    """The fresh cached row of each search, keyed by audience, where it was made with the same query.

    Args:
        location: The pin's Location.
        searches: From :func:`pin_web_searches`.

    Returns:
        ``{audience: row}`` for each search already answered.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    rows = LocationCache.fresh_rows(location.pk, [WEB_SEARCH_SOURCE], [search.scope.audience for search in searches])
    cached: dict[str, LocationCache] = {}
    for search in searches:
        row = rows.get((WEB_SEARCH_SOURCE, search.scope.audience))
        if row is not None and row.query_key == search.query_key:
            cached[search.scope.audience] = row
    return cached


def annotate_results(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Results with the ``domain`` and ``date_display`` the panel shows.

    Args:
        results: The search provider's results.

    Returns:
        The same dicts, annotated in place.
    """
    from urbanlens.dashboard.services.search.search import format_search_date

    annotated = list(results)
    for result in annotated:
        try:
            result["domain"] = urlparse(result.get("link", "")).netloc.removeprefix("www.")
        except (ValueError, AttributeError):
            result["domain"] = ""
        result["date_display"] = format_search_date(result.get("date"))
    return annotated


def store_web_search(location: Location, search: WebSearch, results: list[dict[str, Any]]) -> LocationCache:
    """Cache one search's results under its audience.

    Args:
        location: The pin's Location.
        search: The search that was made.
        results: Its annotated results.

    Returns:
        The saved row.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    data = {"results": results, "search_names": search.scope.provenance()}
    return LocationCache.set(location, WEB_SEARCH_SOURCE, data, query_key=search.query_key, audience=search.scope.audience)


def merged_results(answers: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Several searches' results as one list, in order, each link once.

    Args:
        answers: Each search's results, the shared search's first.

    Returns:
        The combined results.
    """
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for results in answers:
        for result in results:
            identity = str(result.get("link") or json.dumps(result, sort_keys=True, default=str))
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(result)
    return merged
