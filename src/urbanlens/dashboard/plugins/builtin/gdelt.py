"""GDELT plugin: geocoded global news panel for pinned locations, via REData.

REData's ``/search/news/`` already wraps GDELT's DOC 2.0 API (see
``../REData/docs/api-reference.md``, "GET /search/news/ - news-article
search") - there is no local fallback, so this panel is REData-only. One
casualty of the move: REData's endpoint answers only the article list, not
GDELT's separate ``tonechart`` sentiment histogram this panel used to show as
a "coverage leans negative/positive" fact - that mode isn't part of REData's
public contract, so the tone fact is dropped rather than reimplemented here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import InfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource


def _format_gdelt_date(raw: str | None) -> str:
    """Format GDELT's compact ``YYYYMMDDTHHMMSSZ`` ``seendate`` as ``YYYY-MM-DD``.

    REData's normalized news-search results pass this field through
    unparsed (see ``RedataSearchGateway.search_news``); UrbanLens's own,
    now-retired GDELT gateway used to do this same reformatting locally.

    Args:
        raw: The raw ``date`` field from a REData news-search result.

    Returns:
        A ``YYYY-MM-DD`` string, or ``"Undated"`` when ``raw`` is too short
        to contain a date.
    """
    if not raw or len(raw) < 8:
        return "Undated"
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


class GdeltPanelSource(InfoPanelSource):
    """Recent news coverage of the pin's location, via REData's GDELT-backed search."""

    key = "gdelt"
    cache_source = "gdelt"
    section_id = "gdelt-section"
    icon = "newspaper"
    title = "News"

    def gate(self, pin: Pin) -> bool:
        """Requires REData - there is no local GDELT fallback."""
        return redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Search REData's news endpoint for the pin's name and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataNewsSearchGateway

        search_term = pin.get_unique_search_name(quote_name=True)
        articles = RedataNewsSearchGateway().search_news(search_term, max_results=10) if search_term else []
        LocationCache.set(pin.location, self.cache_source, {"articles": articles}, query_key=search_term or "")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the article list from REData's news-search results."""
        articles = (data or {}).get("articles") or []
        if not articles:
            return None

        # ai_extract: news articles are real content pages about the place, so
        # they offer the AI field-extraction button (see _simple_info_panel.html).
        # GDELT has no real snippet, so REData puts the source domain there
        # instead (see RedataSearchGateway.search_news) - used here as the
        # title fallback, same as the old local gateway's "domain" field.
        meta = [
            {"label": _format_gdelt_date(article.get("date")), "value": article.get("title") or article.get("snippet") or "", "href": article.get("link") or "", "ai_extract": True}
            for article in articles[:8]
        ]
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

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the GDELT pin-detail panel."""
        return [GdeltPanelSource()]
