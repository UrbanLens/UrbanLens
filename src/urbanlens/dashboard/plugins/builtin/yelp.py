"""Yelp plugin: business details panel and Media-gallery photos for a pin's location.
Both contributions share one fetch (see :class:`YelpPanelSource`), keyed by coordinates only - never by the pin or wiki's user-given name (see ``services.apis.locations.redata_points_of_interest_gateway`` for why)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, PanelApiKind

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.pins.external_data import PanelSource


def _business_from_poi(poi: dict[str, Any]) -> dict[str, Any]:
    """Map a REData ``yelp`` points-of-interest row onto a Yelp-Fusion-shaped business dict.

    Args:
        poi: One ``PointOfInterestSerializer``-shaped row with ``provider="yelp"``.

    Returns:
        A dict shaped like Yelp Fusion's own business object (``name``, ``url``, ``rating``, ``review_count``, ``price``, ``display_phone``, ``is_closed``, ``categories``, ``photos``, ``image_url``) - the shape ``pin_yelp.html`` and :meth:`YelpPanelSource.media_items` already read."""
    attributes = poi.get("attributes") or {}
    category = poi.get("category") or ""
    photos = attributes.get("photos") or []
    return {
        "name": poi.get("name") or "",
        "url": poi.get("url") or "",
        "rating": attributes.get("rating"),
        "review_count": attributes.get("review_count"),
        "price": attributes.get("price") or "",
        "display_phone": attributes.get("phone") or "",
        "is_closed": attributes.get("is_closed"),
        "categories": [{"title": category}] if category else [],
        "photos": photos,
        "image_url": photos[0] if photos else None,
    }


class YelpPanelSource(GalleryMediaSource):
    """Yelp business info (details panel) and photos (Media gallery tab) for a pin."""

    key = "yelp"
    cache_source = "yelp"
    section_id = "yelp-section"
    icon = "storefront"
    title = "Yelp"
    # Deliberately not exposed on the external API: the Yelp Fusion API's terms restrict
    # redistributing its data beyond direct display to the user who triggered the lookup - serving
    # it through our own bearer-key API is a redistribution question that hasn't been reviewed
    # against those terms, and that question doesn't change just because the data now arrives by way
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()

    def gate(self, pin: Pin) -> bool:
        """Requires REData to be configured and coordinates to search on."""
        if not redata_configured():
            return False
        lat, lng = pin.effective_latitude, pin.effective_longitude
        return bool(lat and lng)

    def fetch(self, pin: Pin) -> None:
        """Search REData's points-of-interest lookup for the nearest Yelp business, then cache it."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway import RedataPointsOfInterestGateway

        lat, lng = pin.effective_latitude, pin.effective_longitude
        data: dict[str, Any] = {}
        query_key = ""
        if lat and lng:
            query_key = f"{lat},{lng}"
            results = RedataPointsOfInterestGateway().find_near(float(lat), float(lng), provider="yelp")
            if results:
                data = {"business": _business_from_poi(results[0]), "reviews": []}
        LocationCache.set(pin.location, self.cache_source, data, query_key=query_key)

    def media_items(self, data: dict) -> list[MediaItem]:
        """Photos Yelp has on file for the business, if any (see the module docstring)."""
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        business = (data or {}).get("business") or {}
        name = business.get("name", "")
        page_url = business.get("url", "")
        return [MediaItem(url=photo_url, thumb_url=photo_url, caption=name, source="Yelp", page_url=page_url) for photo_url in business.get("photos") or []]


class YelpPlugin(UrbanLensPlugin):
    """Yelp business details and photos for pinned locations, via REData."""

    name: ClassVar[str] = "yelp"
    verbose_name: ClassVar[str] = "Yelp"
    description: ClassVar[str] = "Shows Yelp business details (rating, price, phone) for a pin's location, via REData's shared points-of-interest lookup. Requires REData to be configured with a Yelp-enabled key on its own side."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's shared points-of-interest lookup."""
        return {
            "redata_points_of_interest": ServiceDefaults(
                display_name="REData (points of interest)",
                calls_per_minute=120,
                calls_per_day=10000,
                notes="Our own standalone REData service, not a third-party budget - shared with the EPA ECHO plugin (same endpoint, different provider= value).",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the combined Yelp details-panel + Media-gallery source."""
        return [YelpPanelSource()]
