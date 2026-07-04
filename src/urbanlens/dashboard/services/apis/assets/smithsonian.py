from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.core.cache import cache

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(slots=True, kw_only=True)
class SmithsonianGateway(MediaProvider):
    """
    Gateway for the Smithsonian Open Access API.
    """

    service_key: ClassVar[str] = "smithsonian"
    display_name: ClassVar[str] = "Smithsonian Open Access"
    paid_service: ClassVar[bool] = False

    api_key: str
    base_url: str = "https://api.si.edu/openaccess/api/v1.0/search"

    def get_data(self, search_term: str) -> list[dict]:
        cache_key = make_cache_key("smithsonian", search_term)
        # Try to get the data from the cache
        data = cache.get(cache_key)
        # If the data is not in the cache
        if data is None:
            params = {
                "api_key": self.api_key,
                "q": search_term,
                "online_media_type": "Images",
            }
            response = self.session.get(self.base_url, params=params, timeout=60)
            response.raise_for_status()  # Will raise an HTTPError for bad requests

            data = response.json()
            # Store the data in the cache for 24 hours (86400 seconds)
            cache.set(cache_key, data, 86400)
        return self.parse_response(data)

    def get_images_by_coordinates(self, latitude: float, longitude: float) -> list[dict]:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

        # Get the place name from the coordinates
        google_gateway = GoogleGeocodingGateway(api_key=settings.google_unrestricted_api_key)
        place_name = google_gateway.get_place_name(latitude, longitude)

        # Get the images from the Smithsonian API
        return self.get_data(place_name or "")

    def parse_response(self, data: dict) -> list[dict]:
        images = []
        for record in data.get("response", {}).get("rows", []):
            media_list = record.get("content", {}).get("descriptiveNonRepeating", {}).get("online_media", {}).get(
                "media",
            ) or [{}]
            first_media = media_list[0]
            image_data = {
                "title": record.get("title"),
                "url": first_media.get("content"),
                "thumbnail": first_media.get("thumbnail"),
            }
            images.append(image_data)
        return images

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        if not search_term:
            return
        for image in self.get_data(search_term):
            url = image.get("url")
            if not url:
                continue
            yield MediaItem(
                url=url,
                thumb_url=image.get("thumbnail") or url,
                caption=image.get("title") or "",
                source=self.display_name,
            )
