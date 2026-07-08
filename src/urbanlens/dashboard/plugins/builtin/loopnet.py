"""LoopNet plugin: commercial real-estate listings panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class LoopnetPanelSource(LocationCachePanelSource):
    """LoopNet commercial real-estate listings for the pin's address."""

    key = "loopnet"
    cache_source = "loopnet"
    section_id = "loopnet-section"
    icon = "business_center"
    title = "LoopNet Listings"

    @staticmethod
    def address(pin: Pin) -> str:
        """Street + city + state search address, or ``""`` when insufficient.

        Args:
            pin: The pin whose location's address should be assembled.

        Returns:
            A comma-joined address string; empty when the location lacks a
            street route (LoopNet needs at least street-level precision).
        """
        location = pin.location
        if not location or not location.route:
            return ""
        parts = [
            " ".join(filter(None, [location.street_number, location.route])),
            location.locality or "",
            location.administrative_area_level_1 or "",
        ]
        return ", ".join(p for p in parts if p).strip(", ")

    def fetch(self, pin: Pin) -> None:
        """Search LoopNet for the pin's address and cache the listings."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.real_estate.loopnet import LoopNetGateway

        address = self.address(pin)
        result = LoopNetGateway().search(address) if address else None
        LocationCache.set(pin.location, self.cache_source, result or {}, query_key=address)


class LoopnetPlugin(UrbanLensPlugin):
    """LoopNet commercial real-estate listings for pinned locations."""

    name: ClassVar[str] = "loopnet"
    verbose_name: ClassVar[str] = "LoopNet"
    description: ClassVar[str] = "Shows LoopNet commercial real-estate listings for a pin's address on the pin detail page. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for LoopNet."""
        return {
            "loopnet": ServiceDefaults(
                display_name="LoopNet",
                calls_per_minute=5,
                calls_per_day=100,
                usa_only=True,
                notes="US commercial real estate. Scraped - be conservative to avoid blocking.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the LoopNet pin-detail panel."""
        return [LoopnetPanelSource()]
