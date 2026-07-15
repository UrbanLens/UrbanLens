"""GDELT plugin: geocoded global news panel for pinned locations."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class GdeltPanelSource(LocationCachePanelSource):
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
        articles = GdeltGateway().search_articles(search_term, limit=10) if search_term else []
        LocationCache.set(pin.location, self.cache_source, {"articles": articles}, query_key=search_term or "")


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
