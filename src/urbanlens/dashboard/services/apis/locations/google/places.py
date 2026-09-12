from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway


@dataclass(slots=True, kw_only=True)
class GooglePlacesGateway(Gateway):
    """
    Gateway for the Google Places API.
    """

    service_key: ClassVar[str] = "google_places"
    paid_service: ClassVar[bool] = True

    api_key: str

    def get_data(self, latitude, longitude, radius=1000, place_type=None):
        """
        Fetch details about locations near the given coordinates from Google Places API.
        """
        base_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{latitude},{longitude}",
            "radius": radius,
            "key": self.api_key,
        }

        if place_type:
            params["type"] = place_type

        response = self.session.get(base_url, params=params)
        response.raise_for_status()

        return response.json().get("results", [])

    # Places API (New) bills fields by SKU tier - id/displayName/location/types/
    # shortFormattedAddress are Essentials/Pro tier, but rating and userRatingCount are Enterprise
    # tier, billed extra regardless of whether the caller actually uses them.
    # Phone numbers (nationalPhoneNumber/ internationalPhoneNumber) and websiteUri are in this SAME
    _DEFAULT_SEARCH_NEARBY_FIELD_MASK: ClassVar[str] = "places.id,places.displayName,places.location,places.types,places.rating,places.userRatingCount,places.shortFormattedAddress"

    def search_nearby(self, latitude, longitude, radius=2000, included_types=None, max_results=20, field_mask=None):
        """Search nearby places using the new Places API v1 (Nearby Search New).

        Returns:
            List of place dicts, shaped per the requested field_mask."""
        url = "https://places.googleapis.com/v1/places:searchNearby"
        body: dict = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": float(radius),
                },
            },
            "maxResultCount": max(1, min(int(max_results), 20)),
        }
        if included_types:
            body["includedTypes"] = list(included_types)

        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask or self._DEFAULT_SEARCH_NEARBY_FIELD_MASK,
            "Content-Type": "application/json",
        }

        response = self.session.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json().get("places", [])

    def get_place_details(self, place_id, fields):
        """Fetch legacy Place Details fields for a place.

        Returns:
            The place details dict for the requested fields."""
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        params = {
            "place_id": place_id,
            "key": self.api_key,
            "fields": ",".join(fields),
        }

        response = self.session.get(details_url, params=params)
        response.raise_for_status()
        return response.json().get("result", {})

    def find_nearest_place_id(self, latitude: float, longitude: float, radius: float = 75) -> str | None:
        """Find the Google Place id nearest a set of coordinates - never by name.

        Returns:
            The nearest place's id, or None when nothing was found."""
        # Only the id is ever used below - a minimal field_mask avoids paying for the default mask's
        # rating/userRatingCount (Enterprise-tier) fields, which this per-pin lookup (see
        # GoogleMapsPhotosPanelSource) has no use for.
        results = self.search_nearby(latitude, longitude, radius=radius, max_results=1, field_mask="places.id")
        return results[0]["id"] if results else None

    def get_place_photo_names(self, place_id: str, max_photos: int = 10) -> list[str]:
        """Fetch the Places API (New) ``photos[].name`` identifiers for a place.
        ``places/ChIJ.../photos/AelY...``) used with :meth:`get_photo_media` to fetch the actual image bytes - never expose these URLs directly to the browser since resolving them requires the API key.

        Returns:
            Up to ``max_photos`` photo resource names; empty when the place has none on file."""
        url = f"https://places.googleapis.com/v1/places/{place_id}"
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": "photos"}
        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        photos = response.json().get("photos", [])
        return [p["name"] for p in photos[:max_photos] if p.get("name")]

    def get_photo_media(self, photo_name: str, max_width: int = 1200) -> tuple[bytes, str]:
        """Fetch the raw bytes of one Places API (New) photo.
        Server-side only - the API key must never reach the browser.

        Returns:
            Tuple of (image bytes, Content-Type header value)."""
        url = f"https://places.googleapis.com/v1/{photo_name}/media"
        params = {"maxWidthPx": str(max_width), "key": self.api_key}
        response = self.session.get(url, params=params, stream=True)
        response.raise_for_status()
        return response.content, response.headers.get("Content-Type", "image/jpeg")

    def get_place_photos(self, photoreference, max_width=None):
        photo_url = "https://maps.googleapis.com/maps/api/place/photo"
        params = {
            "photoreference": photoreference,
            "key": self.api_key,
        }
        if max_width:
            params["maxwidth"] = max_width

        response = self.session.get(photo_url, params=params, stream=True)
        response.raise_for_status()
        return response.content  # Returns the raw bytes of the image.

    def autocomplete(self, input_text):
        autocomplete_url = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
        params = {
            "input": input_text,
            "key": self.api_key,
        }

        response = self.session.get(autocomplete_url, params=params)
        response.raise_for_status()
        return response.json().get("predictions", [])
