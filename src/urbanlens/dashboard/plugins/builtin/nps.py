"""National Park Service plugin: nearby-park panel on the pin detail page."""

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


class NpsPanelSource(LocationCachePanelSource):
    """National Park Service information for the pin's location."""

    key = "nps"
    cache_source = "nps"
    section_id = "nps-section"
    icon = "park"
    title = "National Park Service"

    def fetch(self, pin: Pin) -> None:
        """Cache NPS details for the park the pin sits inside, if any.

        The panel is about the pinned place *being in* a national park, so the
        result comes from a boundary-containment check -- not a proximity
        search. When the pin falls outside every NPS unit an empty result is
        cached, which keeps the panel hidden.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        park = NPSGateway().find_park_containing_location(lat, lng)
        query_key = f"{lat:.5f},{lng:.5f}"
        LocationCache.set(location, self.cache_source, park or {}, query_key=query_key)


class NpsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the containing-national-park cache (a name/alias source) per Location."""

    key: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    cache_source: ClassVar[str] = "nps"
    service_keys: ClassVar[tuple[str, ...]] = ("nps",)
    usa_only: ClassVar[bool] = True

    def gate(self) -> bool:
        """Requires the NPS API key."""
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        return bool(app_settings.nps_api_key)

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Look up the NPS unit containing a location, if any.

        Args:
            location: The location to check.

        Returns:
            Tuple of (park payload or None, coordinate query key).
        """
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        park = NPSGateway().find_park_containing_location(lat, lng)
        return park, f"{lat:.5f},{lng:.5f}"


class NpsPlugin(UrbanLensPlugin):
    """National Park Service information for pinned locations."""

    name: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    description: ClassVar[str] = "Shows nearby US national park information on the pin detail page. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the NPS API."""
        return {
            "nps": ServiceDefaults(
                display_name="National Park Service API",
                calls_per_minute=10,
                calls_per_day=500,
                usa_only=True,
                notes="Free API. USA only - NPS covers US national parks exclusively.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the NPS pin-detail panel."""
        return [NpsPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the containing park's name as a place-name candidate."""
        return [LocationCacheNameProvider(source="nps", cache_source="nps", keys=("fullName", "name"), verbose_name="National Park Service")]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the containing-park cache to scheduled background enrichment."""
        return [NpsEnrichmentSource()]
