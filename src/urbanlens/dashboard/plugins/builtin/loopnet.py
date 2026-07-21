"""LoopNet plugin: commercial real-estate listings panel on the pin detail page.

Retrieval lives entirely in REData (the standalone service that already owns
property records for this app - see ``plugins.builtin.property_records``):
``RedataGateway.lookup_parcel_uuid`` resolves the pin's address to a parcel,
then ``lookup_listings`` returns REData's cached LoopNet data for it (REData
never scrapes LoopNet inline with the request - see its own docs). Listing
photos are exposed to the pin's Media gallery via :meth:`LoopnetPanelSource.media_items`,
streamed through :class:`~urbanlens.dashboard.controllers.pin.PinLoopnetPhotoView`
so REData's API key never reaches the browser (the same reasoning as every
other authenticated media proxy in this app, e.g. Immich's thumbnail view).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.external_data import PanelSource

logger = logging.getLogger(__name__)


class LoopnetPanelSource(GalleryMediaSource):
    """LoopNet commercial real-estate listings for the pin's address, via REData."""

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

    def gate(self, pin: Pin) -> bool:
        """Skip scheduling a fetch for a pin with no usable address."""
        return bool(self.address(pin))

    def fetch(self, pin: Pin) -> None:
        """Resolve the pin's parcel and cache its LoopNet listings from REData."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        address = self.address(pin)
        location = pin.location
        lat = float(location.latitude) if location and location.latitude is not None else None
        lng = float(location.longitude) if location and location.longitude is not None else None
        if lat is None or lng is None:
            LocationCache.set(pin.location, self.cache_source, {}, query_key=address)
            return

        try:
            gateway = RedataGateway()
            parcel_uuid = gateway.lookup_parcel_uuid(lat, lng, situs_address=address)
            if not parcel_uuid:
                LocationCache.set(pin.location, self.cache_source, {}, query_key=address)
                return
            listings_body = gateway.lookup_listings(parcel_uuid)
        except (PropertyRecordsUnavailableError, ValueError):
            logger.debug("LoopnetPanelSource.fetch: no listings available for pin %s (address=%r)", pin.pk, address, exc_info=True)
            LocationCache.set(pin.location, self.cache_source, {}, query_key=address)
            return

        listings = listings_body.get("results") or []
        data = {"listings": listings} if listings else {}
        LocationCache.set(pin.location, self.cache_source, data, query_key=address)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached LoopNet listing photos into gallery items.

        Args:
            data: This source's cached ``{"listings": [...]}`` dict.

        Returns:
            One item per listing photo, proxied through
            ``PinLoopnetPhotoView`` (never a raw REData URL - the API key
            can't reach the browser).
        """
        from django.urls import reverse

        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        items: list[MediaItem] = []
        for listing in data.get("listings") or []:
            listing_uuid = listing.get("uuid")
            page_url = listing.get("loopnet_url") or ""
            caption = listing.get("title") or ""
            if not listing_uuid:
                continue
            for photo in listing.get("photos") or []:
                photo_id = photo.get("id")
                if photo_id is None:
                    continue
                proxy_url = reverse("pin.loopnet.photo", args=[listing_uuid, photo_id])
                items.append(MediaItem(url=proxy_url, thumb_url=proxy_url, caption=caption, source="LoopNet", page_url=page_url))
        return items


class LoopnetPlugin(UrbanLensPlugin):
    """LoopNet commercial real-estate listings for pinned locations, via REData."""

    name: ClassVar[str] = "loopnet"
    verbose_name: ClassVar[str] = "LoopNet"
    description: ClassVar[str] = "Shows LoopNet commercial real-estate listings (and their photos, in the Media section) for a pin's address, via REData. USA only."
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - this plugin calls REData's own API
    # (service key "redata_api"), already registered by plugins.builtin.property_records.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the LoopNet pin-detail panel (also a Media-gallery source)."""
        return [LoopnetPanelSource()]
