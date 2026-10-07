"""LoopNet plugin: commercial real-estate listings panel on the Private Pin page."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.media.previews import tile_preview_url
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, PanelApiKind
from urbanlens.dashboard.services.pins.redata_panel import RedataBackedSource
from urbanlens.dashboard.services.security.redact import redact_text

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

#: How soon a parcel whose listings REData has only just queued a fetch for is asked again. The fetch runs in REData's
#: background queue, paced against LoopNet's own budget, so minutes rather than seconds.
_REFRESH_RECHECK_SECONDS = 15 * 60


class LoopnetPanelSource(RedataBackedSource, GalleryMediaSource):
    """LoopNet commercial real-estate listings for the pin's address, via REData."""

    key = "loopnet"
    cache_source = "loopnet"
    section_id = "loopnet-section"
    icon = "business_center"
    title = "LoopNet Listings"
    # Deliberately not exposed on the external API: commercial listing data sourced through REData's
    # licensed LoopNet access, plus its photos only resolve through the session-authenticated
    # PinLoopnetPhotoView proxy - an external credential couldn't load them anyway.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()

    @staticmethod
    def address(pin: Pin) -> str:
        """Street + city + state search address, or ``""`` when insufficient.

        Args:
            pin: The pin whose location's address should be assembled.

        Returns:
            A comma-joined address string; empty when the location lacks a street route (LoopNet needs at least street-level precision).
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
        """Requires an address for LoopNet to search by (and, through :class:`RedataBackedSource`, REData)."""
        return bool(self.address(pin)) and super().gate(pin)

    def has_content(self, data: dict | None) -> bool:
        """Whether REData found any listing for the parcel."""
        return bool(data and data.get("listings"))

    def fetch(self, pin: Pin) -> None:
        """Resolve the pin's parcel and cache its LoopNet listings from REData.

        Raises:
            PropertyRecordsUnavailableError: REData could not be asked, so there is no answer to cache.
            PropertyRecordsBusyError: REData holds no listings yet and has queued a fetch, so the parcel is asked again soon.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsBusyError, PropertyRecordsUnavailableError, RedataGateway

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
        except PropertyRecordsUnavailableError as exc:
            if exc.is_outage:
                raise
            logger.debug("LoopnetPanelSource.fetch: no listings available for pin %s (address=%s)", pin.pk, redact_text(address), exc_info=True)
            LocationCache.set(pin.location, self.cache_source, {}, query_key=address)
            return

        listings = listings_body.get("results") or []
        if not listings and listings_body.get("refresh_queued"):
            # REData never fetches inline: an empty answer with a fetch queued means "not looked yet".
            raise PropertyRecordsBusyError("refresh_queued", "REData has queued a LoopNet fetch for this parcel.", retry_after=_REFRESH_RECHECK_SECONDS)
        data = {"listings": listings} if listings else {}
        LocationCache.set(pin.location, self.cache_source, data, query_key=address)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached LoopNet listing photos into gallery items.

        Args:
            data: This source's cached ``{"listings": [...]}`` dict.

        Returns:
            One item per listing photo, proxied through ``PinLoopnetPhotoView`` (never a raw REData URL - the API key can't reach the browser).
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
                items.append(MediaItem(url=proxy_url, thumb_url=tile_preview_url(proxy_url), caption=caption, source="LoopNet", page_url=page_url))
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
