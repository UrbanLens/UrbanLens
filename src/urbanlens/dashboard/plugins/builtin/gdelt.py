"""GDELT plugin: geocoded global news panel for pinned locations, via REData.
REData's ``/search/news/`` already wraps GDELT's DOC 2.0 API (see ``../REData/docs/api-reference.md``, "GET /search/news/ - news-article search") - there is no local fallback, so this panel is REData-only."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, NameSearchSource
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource
    from urbanlens.dashboard.services.pins.search_names import SearchScope

logger = logging.getLogger(__name__)

#: Asked for more than are shown, since the relevance filter drops some.
_REQUESTED_ARTICLES = 25
_SHOWN_ARTICLES = 8


def _format_gdelt_date(raw: str | None) -> str:
    """Format GDELT's compact ``YYYYMMDDTHHMMSSZ`` ``seendate`` as ``YYYY-MM-DD``.

    Args:
        raw: The raw ``date`` field from a REData news-search result.

    Returns:
        A ``YYYY-MM-DD`` string, or ``"Undated"`` when ``raw`` is too short to contain a date."""
    if not raw or len(raw) < 8:
        return "Undated"
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


class GdeltPanelSource(NameSearchSource, InfoPanelSource):
    """Recent news coverage of the pin's location, via REData's GDELT-backed search."""

    key = "gdelt"
    #: Versioned: a row written under an older query or filter is never read.
    cache_source = "gdelt_v2"
    section_id = "gdelt-section"
    icon = "newspaper"
    title = "News"
    results_key = "articles"
    results_per_row = _SHOWN_ARTICLES

    def gate(self, pin: Pin) -> bool:
        """Requires REData - there is no local GDELT fallback."""
        return redata_configured()

    def fetch_scope(self, pin: Pin, scope: SearchScope) -> None:
        """Search REData's news endpoint for the place by one search's names and cache the articles about it."""
        from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataNewsSearchGateway
        from urbanlens.dashboard.services.pins.news_query import NewsQuery

        query = NewsQuery.for_pin(pin, scope)
        if query is None:
            self.store(pin, scope, {"articles": []}, query_key="")
            return
        query_text = query.gdelt_query()
        logger.info("News search for pin %s: %s", pin.pk, query_text)
        answer = RedataNewsSearchGateway().search_news(query_text, max_results=_REQUESTED_ARTICLES)
        # SearXNG standing in for GDELT is a floor, which the cache keeps only briefly.
        self.store(pin, scope, answer.marked({"articles": query.relevant(answer.results)}), query_key=query_text)

    def result_identity(self, result: object) -> str:
        """An article is its link."""
        if isinstance(result, dict) and result.get("link"):
            return str(result["link"])
        return super().result_identity(result)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the article list, keeping only articles about the place under its current names."""
        from urbanlens.dashboard.services.pins.news_query import NewsQuery

        queries = [query for scope in self.search_scopes(pin) if (query := NewsQuery.for_pin(pin, scope)) is not None]
        candidates = (data or {}).get("articles") or []
        articles = [article for article in candidates if isinstance(article, dict) and any(query.is_relevant(article) for query in queries)]
        if not articles:
            return None

        # ai_extract: news articles are real content pages about the place, so they offer the AI
        # field-extraction button (see _simple_info_panel.html).
        meta = [{"label": _format_gdelt_date(article.get("date")), "value": article.get("title") or article.get("snippet") or "", "href": article.get("link") or "", "ai_extract": True} for article in articles]
        return {"meta": meta}

    def debug_count(self, data: dict) -> int:
        """Number of articles found."""
        return len((data or {}).get("articles") or [])


class GdeltPlugin(UrbanLensPlugin):
    """GDELT geocoded global news search for pinned locations, via REData."""

    name: ClassVar[str] = "gdelt"
    verbose_name: ClassVar[str] = "GDELT News"
    description: ClassVar[str] = "Recent news coverage mentioning the pin's location, via REData's GDELT-backed news search."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for redata_search_news."""
        return {
            "redata_search_news": ServiceDefaults(
                display_name="REData News Search",
                category=EgressCategory.REDATA,
                calls_per_minute=20,
                calls_per_day=None,
                notes="GDELT-backed news search via GET /search/news/. Shares REData's one 1,000/hour lookup pool per key. See services.apis.locations.redata_search_gateway.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the GDELT pin-detail panel."""
        return [GdeltPanelSource()]
