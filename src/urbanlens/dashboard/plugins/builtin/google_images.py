"""Google Images plugin: a Media-gallery photo tab sourced from Google Image Search, via REData.

Searched by the pin's address only - never by its user-given name, which may
be a nickname or physical description that doesn't match anything Google
would associate with the place. REData's ``/search/web/?images=true`` only
ever tries providers with an image mode (today, Google Programmable Search),
so this panel keeps its historical name/slug (``google_images``) for
``UL_DISABLED_PLUGINS`` continuity even though it no longer holds its own
Google Custom Search credentials - REData does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, PanelApiKind

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.pins.external_data import PanelSource

_MAX_IMAGES = 10


class GoogleImagesPanelSource(GalleryMediaSource):
    """Up to 10 Google Image Search results for a pin's address, via REData."""

    key = "google_images"
    cache_source = "google_images"
    icon = "image_search"
    title = "Google Images"
    # Deliberately not exposed on the external API: Google Custom Search's
    # terms restrict redistributing image-search results beyond direct
    # display to the user who triggered the search.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()

    def gate(self, pin: Pin) -> bool:
        """Requires REData (which holds the Google Custom Search credentials) and an address to search on."""
        return redata_configured() and bool(pin.effective_address)

    def fetch(self, pin: Pin) -> None:
        """Run a REData image search for the pin's street address and cache it."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataSearchGateway

        address = pin.effective_address or ""
        results: list[dict] = []
        if address:
            try:
                results = RedataSearchGateway().search_web(address, images=True, max_results=_MAX_IMAGES)
            except LocationContextUnavailableError as exc:
                # Quota exhaustion, misconfiguration, or a REData-side outage -
                # degrade to "no results" rather than failing the whole Media
                # gallery loader.
                import logging

                logging.getLogger(__name__).warning("REData image search failed for %r: %s", address, exc)
        LocationCache.set(pin.location, self.cache_source, {"items": results}, query_key=address)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from the cached search results.

        REData's image-mode results carry the image itself under
        ``thumbnail`` and the page it was found on under ``link`` (see
        ``RedataSearchGateway.search_web``) - there is no separate smaller
        preview, so the same URL serves as both the item and its thumbnail.
        """
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        items = (data or {}).get("items") or []
        return [MediaItem(url=r["thumbnail"], thumb_url=r["thumbnail"], caption=r.get("title") or "", source="Google Images", page_url=r.get("link") or r["thumbnail"]) for r in items[:_MAX_IMAGES] if r.get("thumbnail")]


class GoogleImagesPlugin(UrbanLensPlugin):
    """Google Image Search results for pinned locations, via REData."""

    name: ClassVar[str] = "google_images"
    verbose_name: ClassVar[str] = "Google Images"
    description: ClassVar[str] = "Adds up to 10 Google Image Search results (by address) to the pin detail page's Media gallery, via REData's web-search endpoint."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Google Images Media-gallery provider."""
        return [GoogleImagesPanelSource()]
