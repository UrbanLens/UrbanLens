"""GDELT plugin: geocoded global news panel for pinned locations."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import InfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class GdeltPanelSource(InfoPanelSource):
    """Recent news coverage of the pin's location, via GDELT."""

    key = "gdelt"
    cache_source = "gdelt"
    section_id = "gdelt-section"
    icon = "newspaper"
    title = "News"

    def fetch(self, pin: Pin) -> None:
        """Search GDELT for news mentioning the pin's name and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.search.gdelt import GdeltGateway

        search_term = pin.get_unique_search_name(quote_name=True)
        gateway = GdeltGateway()
        articles = gateway.search_articles(search_term, limit=10) if search_term else []
        tone = gateway.get_tone_summary(search_term) if search_term else None
        LocationCache.set(pin.location, self.cache_source, {"articles": articles, "tone": tone}, query_key=search_term or "")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the article list from GDELT's name-search results."""
        articles = (data or {}).get("articles") or []
        if not articles:
            return None

        meta = [
            {"label": article.get("date") or "Undated", "value": article.get("title") or article.get("domain") or "", "href": article.get("url") or ""}
            for article in articles[:8]
        ]

        facts = []
        tone = (data or {}).get("tone")
        if tone:
            average_tone = tone.get("average_tone") or 0
            if average_tone <= -5:
                skew = "leans negative"
            elif average_tone >= 5:
                skew = "leans positive"
            else:
                skew = "neutral"
            facts.append({"icon": "sentiment_satisfied", "text": f"Coverage tone {skew} (avg {average_tone:+.1f} across {tone.get('article_count')} articles)"})

        return {"facts": facts, "meta": meta}

    def debug_count(self, data: dict) -> int:
        """Number of articles found."""
        return len((data or {}).get("articles") or [])


class GdeltPlugin(UrbanLensPlugin):
    """GDELT geocoded global news search for pinned locations."""

    name: ClassVar[str] = "gdelt"
    verbose_name: ClassVar[str] = "GDELT News"
    description: ClassVar[str] = (
        "Free, keyless global news search (GDELT Project DOC 2.0 API) - shows recent news coverage "
        "mentioning the pin's location."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the GDELT DOC API."""
        return {
            "gdelt": ServiceDefaults(
                display_name="GDELT News",
                calls_per_minute=10,
                calls_per_day=500,
                notes="Free, keyless API. Be conservative - shared public infrastructure.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the GDELT pin-detail panel."""
        return [GdeltPanelSource()]
