"""Gateway for REData's ``/search/web/`` and ``/search/news/`` endpoints."""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    REASON_ALL_PROVIDERS_UNAVAILABLE,
    LocationContextEnvelope,
    LocationContextUnavailableError,
    RedataLocationContextGateway,
    refused_as_rejection,
)
from urbanlens.dashboard.services.core.input_validation import require_query

_WEB_SEARCH_PATH = "/api/v1/search/web/"
_NEWS_SEARCH_PATH = "/api/v1/search/news/"


def _extract_results(body: Any) -> list[dict[str, Any]]:
    """Pull the ``results`` list out of a search response body, defensively.

    Args:
        body: The raw decoded JSON body from :meth:`RedataLocationContextGateway.get_json`.

    Returns:
        The ``results`` list, or ``[]`` when the body isn't shaped as expected."""
    if not isinstance(body, dict):
        return []
    results = body.get("results")
    return list(results) if isinstance(results, list) else []


def _news_answer(body: Any) -> LocationContextEnvelope:
    """A ``/search/news/`` body as an envelope.

    From REData 0.3.7 the body says whether GDELT answered (``complete``) and what each provider did (``providers``).
    A body without them, from an older REData, is read as complete, as it always was.

    Args:
        body: The raw decoded JSON body.

    Returns:
        The envelope.
    """
    results = _extract_results(body)
    if not isinstance(body, dict):
        return LocationContextEnvelope(count=len(results), complete=True, results=results)
    providers = body.get("providers")
    outcomes = [entry for entry in providers if isinstance(entry, dict)] if isinstance(providers, list) else []
    return LocationContextEnvelope(count=len(results), complete=body.get("complete") is not False, results=results, providers=outcomes)


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
            Result dicts (``title``, ``link``, ``snippet``, ``date``, ``thumbnail``) from whichever provider answered.

        Raises:
            LocationContextUnavailableError: Every provider REData tried failed to answer, or the request to REData failed outright.
        """
        with refused_as_rejection():
            require_query(type(self).service_key or "redata_search_web", query, name="q")
        params: dict[str, Any] = {"q": query, "limit": max_results}
        if images:
            params["images"] = "true"
        return _extract_results(self.get_json(_WEB_SEARCH_PATH, params))

    def search_news(self, query: str, *, max_results: int = 10, months: int | None = None) -> LocationContextEnvelope:
        """Search recent news coverage through REData's news chain: GDELT, then SearXNG's news category.

        Args:
            query: The search string.
            max_results: Maximum number of articles to request.
            months: How many months back to search. Omit to let REData use
                its own default.

        Returns:
            The answer. Its ``results`` are dicts (``title``, ``link``, ``snippet``, ``date``, ``thumbnail``),
            recency-weighted. ``complete`` is False when SearXNG answered because GDELT did not, so the articles are a
            floor and ``unanswered_sources`` names GDELT.

        Raises:
            LocationContextUnavailableError: Every provider failed, SearXNG found nothing while GDELT was not heard
                from, or the request to REData failed outright.
        """
        with refused_as_rejection():
            require_query(type(self).service_key or "redata_search_web", query, name="q")
        params: dict[str, Any] = {"q": query, "limit": max_results}
        if months is not None:
            params["months"] = months
        answer = _news_answer(self.get_json(_NEWS_SEARCH_PATH, params))
        if not answer.complete and not answer.results:
            # The fallback's empty page says nothing about coverage GDELT was not asked for.
            raise LocationContextUnavailableError(REASON_ALL_PROVIDERS_UNAVAILABLE, "GDELT did not answer, and REData's news fallback found nothing.")
        return answer


class RedataNewsSearchGateway(RedataSearchGateway):
    """:class:`RedataSearchGateway` rate-limited/cost-tracked under its own service key."""

    service_key: ClassVar[str] = "redata_search_news"
