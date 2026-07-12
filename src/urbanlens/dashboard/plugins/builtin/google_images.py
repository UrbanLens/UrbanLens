"""Google Images plugin: a Media-gallery photo tab sourced from Google Image Search.

Searched by the pin's address only - never by its user-given name, which may
be a nickname or physical description that doesn't match anything Google
would associate with the place. Shares credentials and rate-limit accounting
with the existing ``google_search`` (Custom Search JSON API) service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.external_data import PanelSource

_MAX_IMAGES = 10


class GoogleImagesPanelSource(GalleryMediaSource):
    """Up to 10 Google Image Search results for a pin's address."""

    key = "google_images"
    cache_source = "google_images"
    icon = "image_search"
    title = "Google Images"

    def gate(self, pin: Pin) -> bool:
        """Requires configured Custom Search credentials and an address to search on."""
        from urbanlens.UrbanLens.settings.app import settings

        return bool(settings.google_domain_restricted_api_key and settings.google_search_tenant and pin.effective_address)

    def fetch(self, pin: Pin) -> None:
        """Run a Google Image Search for the pin's street address and cache it."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchError
        from urbanlens.dashboard.services.apis.search.google_images import GoogleImageSearchGateway

        address = pin.effective_address or ""
        results: list[dict] = []
        if address:
            try:
                results = GoogleImageSearchGateway().search_images(address, max_results=_MAX_IMAGES)
            except GoogleCustomSearchError as exc:
                # Quota exhaustion or misconfiguration - degrade to "no results"
                # rather than failing the whole Media gallery loader.
                import logging

                logging.getLogger(__name__).warning("Google Image Search failed for %r: %s", address, exc)
        LocationCache.set(pin.location, self.cache_source, {"items": results}, query_key=address)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from the cached search results."""
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        items = (data or {}).get("items") or []
        return [
            MediaItem(url=r["link"], thumb_url=r.get("thumbnail") or r["link"], caption=r.get("title") or "", source="Google Images", page_url=r.get("context_link") or r["link"])
            for r in items[:_MAX_IMAGES]
            if r.get("link")
        ]


class GoogleImagesPlugin(UrbanLensPlugin):
    """Google Image Search results for pinned locations."""

    name: ClassVar[str] = "google_images"
    verbose_name: ClassVar[str] = "Google Images"
    description: ClassVar[str] = "Adds up to 10 Google Image Search results (by address) to the pin detail page's Media gallery."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Google Images Media-gallery provider."""
        return [GoogleImagesPanelSource()]
