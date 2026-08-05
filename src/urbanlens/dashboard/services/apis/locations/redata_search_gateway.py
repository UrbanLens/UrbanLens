"""Gateway for REData's ``/search/web/`` and ``/search/news/`` endpoints.

See ``../REData/docs/api-reference.md``, "GET /search/web/ - web search" and
"GET /search/news/ - news-article search". Replaces UrbanLens's own local
provider fallback chain (SearXNG, Brave, Mojeek, Marginalia, Google
Programmable Search, DuckDuckGo) and its separate GDELT news gateway with
REData's already-pooled versions of the same two searches - REData runs the
identical "try each provider in order, first to answer wins" chain
server-side, so duplicating it locally bought nothing but a second set of
provider credentials to manage.

Both endpoints answer the plain ``{"count", "results"}`` envelope (no
``providers`` block - see :meth:`RedataLocationContextGateway.get_json`),
with each result dict already normalized to ``title``/``link``/``snippet``/
``date``/``thumbnail`` - the same shape UrbanLens's own local provider
gateways used to produce, so callers on this side needed no changes.
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_WEB_SEARCH_PATH = "/api/v1/search/web/"
_NEWS_SEARCH_PATH = "/api/v1/search/news/"


def _extract_results(body: Any) -> list[dict[str, Any]]:
    """Pull the ``results`` list out of a search response body, defensively.

    Args:
        body: The raw decoded JSON body from
            :meth:`RedataLocationContextGateway.get_json`.

    Returns:
        The ``results`` list, or ``[]`` when the body isn't shaped as expected.
    """
    if not isinstance(body, dict):
        return []
    results = body.get("results")
    return list(results) if isinstance(results, list) else []


class RedataSearchGateway(RedataLocationContextGateway):
    """REST client for REData's general web-search endpoint (and, via
    :meth:`search_news`, its separate GDELT-backed news endpoint)."""

    service_key: ClassVar[str] = "redata_search_web"

    def search_web(self, query: str, *, max_results: int = 10, images: bool = False) -> list[dict[str, Any]]:
        """Search the web through REData's ordered provider fallback chain.

        Args:
            query: The search string.
            max_results: Maximum number of results to request.
            images: Restrict to providers with an image mode (today, only
                Google Programmable Search) and return image results instead
                of ordinary web results. In this mode REData's normalized
                ``link`` is the page the image was found on and ``thumbnail``
                is the image itself - there is no separate smaller preview.

        Returns:
            Result dicts (``title``, ``link``, ``snippet``, ``date``,
            ``thumbnail``) from whichever provider answered. Empty when
            nothing matched.

        Raises:
            LocationContextUnavailableError: Every provider REData tried
                failed to answer, or the request to REData failed outright.
        """
        params: dict[str, Any] = {"q": query, "limit": max_results}
        if images:
            params["images"] = "true"
        return _extract_results(self.get_json(_WEB_SEARCH_PATH, params))

    def search_news(self, query: str, *, max_results: int = 10, months: int | None = None) -> list[dict[str, Any]]:
        """Search recent news coverage through REData's GDELT-backed endpoint.

        Args:
            query: The search string.
            max_results: Maximum number of articles to request.
            months: How many months back to search. Omit to let REData use
                its own default.

        Returns:
            Result dicts (``title``, ``link``, ``snippet``, ``date``,
            ``thumbnail``), recency-weighted. Empty when nothing matched.

        Raises:
            LocationContextUnavailableError: GDELT failed to answer, or the
                request to REData failed outright.
        """
        params: dict[str, Any] = {"q": query, "limit": max_results}
        if months is not None:
            params["months"] = months
        return _extract_results(self.get_json(_NEWS_SEARCH_PATH, params))


class RedataNewsSearchGateway(RedataSearchGateway):
    """:class:`RedataSearchGateway` rate-limited/cost-tracked under its own service key.

    Same client and endpoints as :class:`RedataSearchGateway`; this subclass
    exists purely so the "News" pin-detail panel's call volume is tracked
    separately in UrbanLens's own cost dashboard from the general Web Search
    panel and the two image-search panels, all of which share
    ``redata_search_web``.
    """

    service_key: ClassVar[str] = "redata_search_news"
