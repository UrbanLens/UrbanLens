"""REData place-details plugin: a compact Google Maps info card for CID-linked locations.
This plugin reads it back via a second, independent REData endpoint - ``RedataCidGateway.get_place_detail`` (``GET /places/cid/{cid}/``, a pure database read that never triggers a new scrape - see that method's docstring)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.media.previews import tile_preview_url
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, GalleryMediaSource, PanelApiKind

if TYPE_CHECKING:
    from django.db.models import Q

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: How soon a CID REData had never seen is asked about again, once it has been posted for resolution.
UNKNOWN_CID_STALE_AFTER = timedelta(days=1)
#: Marks a cached answer as "posted for resolution, not answered yet" rather than "no details".
_RESOLUTION_PENDING = "resolution_pending"

#: Human-readable provider name for every MediaItem this plugin emits.
_SOURCE_NAME = "Google Maps (REData)"

#: Photos are the one part of the deep scrape worth showing on a compact card, and even those are
#: capped - REData can return dozens per popular place, and this panel's job is a quick glance, not a
#: Google Maps clone (see the module docstring).
_MAX_PHOTOS = 3


def place_detail(cid: int) -> dict[str, Any]:
    """REData's place detail for a CID, posting a CID it has never seen for resolution.

    REData answers ``404`` for a CID nobody has posted to ``POST /places/resolve-cids/``; a place with
    no location is a confirmed answer from that endpoint instead.

    Args:
        cid: The Google Maps CID.

    Returns:
        The detail payload; ``{}`` when REData confirmed the CID has no place; or a
        :data:`_RESOLUTION_PENDING` marker while REData resolves it.

    Raises:
        GatewayRequestError: REData could not be asked.
    """
    from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway

    gateway = RedataCidGateway()
    detail = gateway.get_place_detail(cid)
    if detail is not None:
        return detail
    result = gateway.resolve_cids([cid])
    if cid in result.unresolvable:
        return {}
    if cid in result.resolved:
        return gateway.get_place_detail(cid) or {_RESOLUTION_PENDING: True}
    return {_RESOLUTION_PENDING: True}


def _stale_after(data: dict | None) -> timedelta | None:
    return UNKNOWN_CID_STALE_AFTER if data and data.get(_RESOLUTION_PENDING) else None


def _coerce_cid(cid_value: Any) -> int | None:
    """Coerce a cached payload's ``cid`` field to ``int``, or None when unusable."""
    if cid_value is None:
        return None
    try:
        return int(cid_value)
    except (TypeError, ValueError):
        return None


class RedataPlaceDetailsPanelSource(CoordinateGatedInfoPanelSource, GalleryMediaSource):
    """A compact Google Maps info card (plus up to 3 photos) for a CID-linked pin."""

    key = "redata_place_details"
    cache_source = "redata_place_details"
    section_id = "redata-place-details-section"
    icon = "storefront"
    title = "Google Maps Details"
    # Both an info card and a media provider - see _MAX_PHOTOS above for why.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO, PanelApiKind.MEDIA})
    required_feature: ClassVar[SiteFeature | None] = SiteFeature.PLACES

    def gate(self, pin: Pin) -> bool:
        """Requires REData to be configured and this pin's location to already carry a CID.
        Deliberately not the inherited coordinate/``geo_boundary`` gate: a pin can have coordinates with no linked CID (most of them - only CID-link imports set one), and this panel has nothing to fetch without one, no matter where the pin sits."""
        location = pin.location
        return redata_configured() and location is not None and location.cid is not None

    def fetch(self, pin: Pin) -> None:
        """Read REData's cached deep-scrape record for the pin's CID."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        location = pin.location
        cid = location.cid if location is not None else None
        if cid is None:
            LocationCache.set(pin.location, self.cache_source, {}, query_key="")
            return

        cid_int = int(cid)
        detail = place_detail(cid_int)
        LocationCache.set(pin.location, self.cache_source, detail, query_key=str(cid_int), stale_after=_stale_after(detail))

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the compact card from a cached place-detail payload."""
        name = str(data.get("name") or "").strip()
        if not name:
            return None

        meta: list[dict[str, str]] = []
        rating = data.get("rating")
        if isinstance(rating, (int, float)):
            rating_value = f"{rating:g}"
            review_count = data.get("review_count")
            if isinstance(review_count, int) and review_count:
                rating_value = f"{rating_value} ({review_count:,} reviews)"
            meta.append({"label": "Rating", "value": rating_value})
        if price_level := str(data.get("price_level") or "").strip():
            meta.append({"label": "Price", "value": price_level})
        hours = data.get("hours")
        if isinstance(hours, dict) and (summary := str(hours.get("summary") or "").strip()):
            meta.append({"label": "Hours", "value": summary})
        if phone := str(data.get("phone_number") or "").strip():
            meta.append({"label": "Phone", "value": phone, "href": f"tel:{phone}"})

        website = str(data.get("website") or "").strip()
        return {
            "heading_name": name,
            "chips": [str(data.get("category") or "").strip() or None],
            "meta": meta,
            "footer_link": {"url": website, "label": "Visit website"} if website else None,
        }

    def media_items(self, data: dict) -> list[MediaItem]:
        """Up to :data:`_MAX_PHOTOS` of the cached record's ``media`` entries where ``kind == "photo"``."""
        from django.urls import reverse

        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        cid = _coerce_cid(data.get("cid"))
        if cid is None:
            return []

        name = str(data.get("name") or "")
        items: list[MediaItem] = []
        for media in data.get("media") or []:
            if not isinstance(media, dict) or media.get("kind") != "photo":
                continue
            media_id = media.get("id")
            if media_id is None:
                continue
            proxy_url = reverse("pin.place_cid.media", args=[cid, media_id])
            items.append(MediaItem(url=proxy_url, thumb_url=tile_preview_url(proxy_url), caption=name, source=_SOURCE_NAME, content_type=str(media.get("content_type") or "")))
            if len(items) >= _MAX_PHOTOS:
                break
        return items

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The cached record as both an information card and its (up to 3) photos."""
        data = self.cached_data(pin)
        if data is None:
            return None
        card = self.api_info(pin, data)
        media = self.api_media(data)
        if card is None and not media:
            return None
        return {PanelApiKind.INFO.value: card, PanelApiKind.MEDIA.value: media}


class RedataPlaceDetailsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the REData place-detail cache for CID-linked Locations."""

    key: ClassVar[str] = "redata_place_details"
    verbose_name: ClassVar[str] = "Google Maps Details (REData)"
    cache_source: ClassVar[str] = "redata_place_details"

    def gate(self) -> bool:
        """Requires REData to be configured."""
        return redata_configured()

    def missing_filter(self) -> Q:
        """Locations with a resolved CID and no cache row yet for this source."""
        from django.db.models import Q

        return Q(google_place__cid__isnull=False) & super().missing_filter()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Read REData's cached deep-scrape record for the location's CID.

        Raises:
            GatewayRequestError: REData could not be asked, so there is no answer to cache.
        """
        cid = location.cid
        if cid is None:
            return None, ""
        cid_int = int(cid)
        return place_detail(cid_int), str(cid_int)

    def stale_after(self, data: dict | None) -> timedelta | None:
        """A CID still being resolved is asked about again soon rather than kept for the whole window."""
        return _stale_after(data)


class RedataPlaceDetailsPlugin(UrbanLensPlugin):
    """A compact Google Maps info card, via REData's already-run deep scrape of a linked CID."""

    name: ClassVar[str] = "redata_place_details"
    verbose_name: ClassVar[str] = "Google Maps Details (REData)"
    description: ClassVar[str] = (
        "Shows a compact Google Maps info card (rating, price, hours, phone, up to 3 photos) for a pin's location, "
        "read from REData's already-run deep scrape of its linked Google Maps CID. Never triggers a new scrape; "
        "requires the location to already carry a resolved CID."
    )
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - this plugin's gateway (RedataCidGateway, service key
    # "redata_cid_lookup") is already registered directly in rate_limiter.SERVICE_REGISTRY, not
    # through a plugin (see cid_resolution.py's own precedent - it doesn't register defaults
    # either).

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the compact place-details pin-detail panel (also a Media-gallery source)."""
        return [RedataPlaceDetailsPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the place-detail cache to scheduled background enrichment."""
        return [RedataPlaceDetailsEnrichmentSource()]
