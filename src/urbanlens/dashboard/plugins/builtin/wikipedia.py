"""Wikipedia plugin: article summary panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider


class WikipediaPanelSource(LocationCachePanelSource):
    """Wikipedia article summary for the pin's location."""

    key = "wikipedia"
    cache_source = "wikipedia"
    section_id = "wikipedia-section"
    icon = "menu_book"
    title = "Wikipedia"

    def fetch(self, pin: Pin) -> None:
        """Find and cache the best-matching Wikipedia article."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        address_components = {
            "locality": location.locality or "",
            "route": location.route or "",
            "street_number": location.street_number or "",
            "administrative_area_level_1": location.administrative_area_level_1 or "",
        }
        name = pin.meaningful_official_name or pin.meaningful_name or ""
        address_bits = ", ".join(
            filter(
                None,
                [
                    " ".join(filter(None, [location.street_number, location.route])),
                    location.locality,
                    location.administrative_area_level_1,
                ],
            )
        )
        query_key = f"{name} ({address_bits})" if name and address_bits else name or address_bits or f"{lat:.5f}, {lng:.5f}"
        article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name)
        LocationCache.set(location, self.cache_source, article or {}, query_key=query_key)


class WikipediaEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the Wikipedia article cache (a name/alias source) per Location."""

    key: ClassVar[str] = "wikipedia"
    verbose_name: ClassVar[str] = "Wikipedia article"
    cache_source: ClassVar[str] = "wikipedia"
    service_keys: ClassVar[tuple[str, ...]] = ("wikipedia",)

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Find the best-matching Wikipedia article for a location.

        Args:
            location: The location to fetch an article for.

        Returns:
            Tuple of (article payload or None, query key).
        """
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        address_components = {
            "locality": location.locality or "",
            "route": location.route or "",
            "street_number": location.street_number or "",
            "administrative_area_level_1": location.administrative_area_level_1 or "",
        }
        name = location.official_name or ""
        article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name)
        return article, name or f"{lat:.5f}, {lng:.5f}"


class WikipediaPlugin(UrbanLensPlugin):
    """Wikipedia article summaries for pinned locations."""

    name: ClassVar[str] = "wikipedia"
    verbose_name: ClassVar[str] = "Wikipedia"
    description: ClassVar[str] = "Shows the best-matching Wikipedia article for a pin's location on the pin detail page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Wikipedia API."""
        return {
            "wikipedia": ServiceDefaults(
                display_name="Wikipedia",
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Free API. Be polite - set a descriptive User-Agent.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Wikipedia pin-detail panel."""
        return [WikipediaPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the cached article's title as a place-name candidate."""
        return [LocationCacheNameProvider(source="wikipedia", cache_source="wikipedia", keys=("title",), verbose_name="Wikipedia")]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the Wikipedia article cache to scheduled background enrichment."""
        return [WikipediaEnrichmentSource()]
