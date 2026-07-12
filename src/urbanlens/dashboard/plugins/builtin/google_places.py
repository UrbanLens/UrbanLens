"""Google Places plugin: place-name candidates and rate-limit defaults.

Google Places has no pin-detail panel of its own - its details payload is
cached into ``LocationCache`` by the place-details flow (see
``tasks.prefetch_location_external_data``) and the linked ``GooglePlace`` row
carries a resolved place name. This plugin wires both into the plugin system
as name candidates and owns the service's rate-limit defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import GalleryMediaSource
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.external_data import PanelSource


class GoogleMapsPhotosPanelSource(GalleryMediaSource):
    """Photos users have uploaded to Google Maps for a pin's location.

    Found by coordinates only, via Places API (New) Nearby Search - never by
    the pin/wiki's user-given name. Photo bytes are proxied server-side (see
    ``controllers.media_proxy.GoogleMapsPhotoProxyView``) since resolving a
    Places API (New) photo name requires the API key.
    """

    key = "google_maps"
    cache_source = "google_maps_photos"
    icon = "photo_camera"
    title = "Google Maps"

    def gate(self, pin: Pin) -> bool:
        """Requires a configured API key and coordinates."""
        from urbanlens.UrbanLens.settings.app import settings

        return bool(settings.google_unrestricted_api_key) and bool(pin.effective_latitude and pin.effective_longitude)

    def fetch(self, pin: Pin) -> None:
        """Find the nearest place by coordinates and cache its photo names."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
        from urbanlens.UrbanLens.settings.app import settings

        gateway = GooglePlacesGateway(api_key=settings.google_unrestricted_api_key or "")
        lat, lng = pin.effective_latitude, pin.effective_longitude
        place_id = gateway.find_nearest_place_id(lat, lng)
        photo_names = gateway.get_place_photo_names(place_id, max_photos=10) if place_id else []
        LocationCache.set(
            pin.location,
            self.cache_source,
            {"place_id": place_id, "photo_names": photo_names},
            query_key=f"{lat},{lng}",
        )

    def media_items(self, data: dict) -> list[MediaItem]:
        """Build proxied media items from the cached photo names."""
        from urllib.parse import quote

        from django.urls import reverse

        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        place_id = (data or {}).get("place_id") or ""
        page_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else ""
        items = []
        for photo_name in (data or {}).get("photo_names") or []:
            proxy_url = reverse("media.google_maps_photo", args=[quote(photo_name, safe="")])
            items.append(MediaItem(url=proxy_url, thumb_url=proxy_url, caption="", source="Google Maps", page_url=page_url or proxy_url))
        return items


class GooglePlacesNameProvider(NameProvider):
    """Google place names from the linked GooglePlace row and cached Places details."""

    def __init__(self) -> None:
        """Initialize with the ``google_places`` source slug."""
        super().__init__(source="google_places", verbose_name="Google Places")

    def candidates(self, location: Location) -> list[str | None]:
        """Return the cached Google place name and the cached details name.

        Args:
            location: The location to name.

        Returns:
            Raw candidate values; empty entries are filtered by the caller.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        values: list[str | None] = [location.cached_place_name]
        cached = LocationCache.get_fresh(location, "google_places")
        data = cached.data if cached else None
        if isinstance(data, dict):
            values.append(data.get("name"))
        return values


class GooglePlacesPlugin(UrbanLensPlugin):
    """Google Places integration: place names and service defaults."""

    name: ClassVar[str] = "google_places"
    verbose_name: ClassVar[str] = "Google Places"
    description: ClassVar[str] = "Provides place names for locations from the Google Places API."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 10

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Google Places API."""
        return {
            "google_places": ServiceDefaults(
                display_name="Google Places API",
                calls_per_minute=20,
                calls_per_day=200,
                notes="Free tier: $200/month credit. Geocoding/details billed per call.",
            ),
        }

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute Google place names as place-name candidates."""
        return [GooglePlacesNameProvider()]

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Google Maps Media-gallery photos provider."""
        return [GoogleMapsPhotosPanelSource()]
