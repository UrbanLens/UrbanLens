"""Yelp plugin: business details panel and Media-gallery photos for a pin's location.

Both contributions share one fetch (see :class:`YelpPanelSource`), keyed by
coordinates/address only - never by the pin or wiki's user-given name (see
``services.apis.yelp.gateway`` for why).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import GalleryMediaSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.external_data import PanelSource


class YelpPanelSource(GalleryMediaSource):
    """Yelp business info (details panel) and photos (Media gallery tab) for a pin."""

    key = "yelp"
    cache_source = "yelp"
    section_id = "yelp-section"
    icon = "storefront"
    title = "Yelp"

    def gate(self, pin: Pin) -> bool:
        """Requires a configured API key and coordinates or an address to search on."""
        from urbanlens.UrbanLens.settings.app import settings

        if not settings.yelp_api_key:
            return False
        lat, lng = pin.effective_latitude, pin.effective_longitude
        return bool((lat and lng) or pin.effective_address)

    def fetch(self, pin: Pin) -> None:
        """Search Yelp by coordinates/address, then cache business details + reviews."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.yelp.gateway import YelpGateway
        from urbanlens.UrbanLens.settings.app import settings

        gateway = YelpGateway(api_key=settings.yelp_api_key or "")
        lat, lng = pin.effective_latitude, pin.effective_longitude
        business = gateway.find_nearest_business(latitude=lat, longitude=lng, address=None if lat and lng else pin.effective_address)

        data: dict = {}
        query_key = f"{lat},{lng}" if lat and lng else (pin.effective_address or "")
        if business:
            details = gateway.get_business(business["id"])
            reviews = gateway.get_reviews(business["id"])
            data = {"business": details, "reviews": reviews}
        LocationCache.set(pin.location, self.cache_source, data, query_key=query_key)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Photos Yelp has on file for the business, if any."""
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        business = (data or {}).get("business") or {}
        name = business.get("name", "")
        page_url = business.get("url", "")
        return [MediaItem(url=photo_url, thumb_url=photo_url, caption=name, source="Yelp", page_url=page_url) for photo_url in business.get("photos") or []]


class YelpPlugin(UrbanLensPlugin):
    """Yelp business details, most recent review, and photos for pinned locations."""

    name: ClassVar[str] = "yelp"
    verbose_name: ClassVar[str] = "Yelp"
    description: ClassVar[str] = "Shows Yelp business details (rating, price, hours, most recent review) and photos for a pin's location. Requires a Yelp Fusion API key."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Yelp Fusion API."""
        return {
            "yelp": ServiceDefaults(
                display_name="Yelp Fusion API",
                calls_per_minute=30,
                calls_per_day=500,
                notes="Free tier: 5,000 calls/day per app. Coordinates/address search only, never by name.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the combined Yelp details-panel + Media-gallery source."""
        return [YelpPanelSource()]
