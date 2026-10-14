"""Resolves Places API calls (nearby search, details, photos, autocomplete) to whichever provider is available."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.apis.locations.google.geocoding import LOCALITY_PLACE_TYPES
from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, ProviderUnavailableError
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.security.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

PROVIDER_GOOGLE = "google_places"

#: The exact legacy Google Place Details fields the pin-detail panel needs - kept as
#: a constant so every provider's get_place_details_full stays obviously in sync with the others.
VIP_DETAIL_FIELDS: tuple[str, ...] = ("name", "formatted_address", "rating", "editorial_summary", "opening_hours", "website", "url", "photos")


class PhotoNotFoundError(Exception):
    """Confirmed no photo exists at this identifier, on whichever provider produced it."""


class PlacesProvider(ABC):
    """
    One source of Places data: nearby search, details, photos and autocomplete, in Places API (New) shapes.

    The first available provider answers every call; there is no fallback after a failure, because the providers
    differ in what they cost and in what they know, and a silent switch would cache one's answers under the other's key.

    Attributes:
        key: Short identifier, used to cache an answer per provider.
        shields_direct: Whether a failure of this provider should stop a chain before it falls through to a provider
            that bills this deployment directly (see ``direct_fallback_permitted``).
    """

    key: ClassVar[str]
    shields_direct: ClassVar[bool] = False

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured to answer."""

    @abstractmethod
    def service_keys(self) -> tuple[str, ...]:
        """The rate-limiter service keys its calls are spent against."""

    @abstractmethod
    def search_nearby_landmarks(self, latitude: float, longitude: float, radius: float, included_types: list[str]) -> list[dict[str, Any]]:
        """
        Places near a coordinate, in Places API (New) shape.

        Returns:
            Dicts with ``id``, ``displayName.text``, ``location.{latitude,longitude}``, ``shortFormattedAddress``, ``types``,
            ``rating`` and ``userRatingCount``.

        Raises:
            GatewayRequestError: The request failed outright.
        """

    @abstractmethod
    def get_place_details_full(self, place_id: str) -> dict[str, Any]:
        """
        Details for the pin-detail panel, in legacy Place Details shape (see :data:`VIP_DETAIL_FIELDS`).

        Raises:
            GatewayRequestError: The request failed outright.
        """

    @abstractmethod
    def find_nearest_place_photos(self, latitude: float, longitude: float) -> tuple[str | None, list[str]]:
        """
        The nearest place's id and up to 10 photo identifiers, each opaque to the caller and claimed by :meth:`owns_photo`.

        Raises:
            GatewayRequestError: The request failed outright.
        """

    @abstractmethod
    def owns_photo(self, photo_identifier: str) -> bool:
        """
        Whether this provider produced ``photo_identifier``.

        Asked of every provider whether or not it is available: an identifier already encodes which provider produced
        it (it was persisted into ``LocationCache``), so a configuration change must not orphan it.
        """

    @abstractmethod
    def download_photo(self, photo_identifier: str) -> tuple[bytes, str]:
        """
        One photo's bytes and content type.

        Raises:
            PhotoNotFoundError: The provider confirmed the photo no longer exists.
            GatewayRequestError: The request failed outright.
        """

    @abstractmethod
    def autocomplete_predictions(self, query: str) -> list[dict[str, str]]:
        """
        Predictions as the user types: ``{"place_id", "main_text", "secondary_text"}`` dicts in the provider's ranked order.

        Raises:
            GatewayRequestError: The request failed outright.
        """

    @abstractmethod
    def resolve_place_coordinates(self, place_id: str) -> tuple[float | None, float | None, str | None]:
        """
        ``(latitude, longitude, name)`` for a place id chosen from autocomplete; each may be None.

        Raises:
            GatewayRequestError: The request failed outright.
        """

    @abstractmethod
    def resolve_name_from_nearby(self, latitude: float, longitude: float, radius: float) -> str | None:
        """The first meaningful nearby place name, or None. Never raises for a failed request."""


class GooglePlacesProvider(PlacesProvider):
    """Google Places, called directly with ``settings.google_unrestricted_api_key``."""

    key: ClassVar[str] = PROVIDER_GOOGLE

    def available(self) -> bool:
        """Whether the unrestricted Google API key is set."""
        return bool(settings.google_unrestricted_api_key)

    def service_keys(self) -> tuple[str, ...]:
        """Google Places' own budget."""
        return ("google_places",)

    def _gateway(self) -> GooglePlacesGateway:
        return GooglePlacesGateway(api_key=settings.google_unrestricted_api_key or "")

    def search_nearby_landmarks(self, latitude: float, longitude: float, radius: float, included_types: list[str]) -> list[dict[str, Any]]:
        """Nearby search, already in Places API (New) shape."""
        return self._gateway().search_nearby(latitude, longitude, radius=radius, included_types=included_types)

    def get_place_details_full(self, place_id: str) -> dict[str, Any]:
        """Place Details with :data:`VIP_DETAIL_FIELDS`."""
        return self._gateway().get_place_details(place_id, fields=list(VIP_DETAIL_FIELDS))

    def find_nearest_place_photos(self, latitude: float, longitude: float) -> tuple[str | None, list[str]]:
        """The nearest place id and up to 10 photo resource names."""
        gateway = self._gateway()
        place_id = gateway.find_nearest_place_id(latitude, longitude)
        photo_names = gateway.get_place_photo_names(place_id, max_photos=10) if place_id else []
        return place_id, photo_names

    def owns_photo(self, photo_identifier: str) -> bool:
        """Every identifier no other provider claims is a Google photo resource name."""
        return True

    def download_photo(self, photo_identifier: str) -> tuple[bytes, str]:
        """Download via ``get_photo_media``; a ``requests`` failure propagates unchanged."""
        return self._gateway().get_photo_media(photo_identifier)

    def autocomplete_predictions(self, query: str) -> list[dict[str, str]]:
        """Normalise Google's structured formatting."""
        results: list[dict[str, str]] = []
        for pred in self._gateway().autocomplete(query):
            fmt = pred.get("structured_formatting") or {}
            results.append(
                {
                    "place_id": pred.get("place_id") or "",
                    "main_text": fmt.get("main_text") or pred.get("description") or "",
                    "secondary_text": fmt.get("secondary_text") or "",
                },
            )
        return results

    def resolve_place_coordinates(self, place_id: str) -> tuple[float | None, float | None, str | None]:
        """Place Details limited to ``geometry`` and ``name`` - nothing a pin-add does not need, so no costlier SKU."""
        details = self._gateway().get_place_details(place_id, fields=["geometry", "name"])
        loc = details.get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        name = details.get("name")
        if lat is not None and lng is not None:
            return float(lat), float(lng), name
        return None, None, name

    def resolve_name_from_nearby(self, latitude: float, longitude: float, radius: float) -> str | None:
        """The first meaningful nearby name; a point Google has nothing at is no name, not a failure."""
        if not self.available():
            return None
        try:
            candidates = self._gateway().get_data(latitude, longitude, radius=radius)
        except (OSError, ValueError, requests.RequestException, RequestCancelledError, ImpossibleInputError) as exc:
            logger.debug("Google Places name lookup failed for %s,%s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return None
        return first_meaningful_name(candidates)


def first_meaningful_name(candidates: list[dict[str, Any]]) -> str | None:
    """The first candidate's name that is not merely a locality, or None."""
    for result in candidates:
        types = set(result.get("types") or [])
        if types and not (types - LOCALITY_PLACE_TYPES):
            continue
        name = (result.get("name") or "").strip()
        if name:
            return name
    return None


def _providers() -> list[PlacesProvider]:
    from urbanlens.dashboard.plugins.points import PLACES_PROVIDERS

    return list(PLACES_PROVIDERS)


def active_places_provider() -> PlacesProvider | None:
    """The first available provider, or None when none is configured."""
    return next((provider for provider in _providers() if provider.available()), None)


def _require_provider() -> PlacesProvider:
    provider = active_places_provider()
    if provider is None:
        raise ProviderUnavailableError("No places provider is configured.")
    return provider


def available() -> bool:
    """Whether any provider can answer place lookups on this install."""
    return active_places_provider() is not None


def active_provider() -> str:
    """
    Which provider answers place lookups on this install, so an answer can be cached per provider.

    Returns:
        The first available provider's ``key``; empty when none is.
    """
    provider = active_places_provider()
    return provider.key if provider is not None else ""


def service_keys() -> tuple[str, ...]:
    """The rate-limiter service keys the active provider spends; empty when none is available."""
    provider = active_places_provider()
    return provider.service_keys() if provider is not None else ()


def shields_direct() -> bool:
    """Whether the active provider's failure should stop a chain before it reaches a provider billed directly."""
    provider = active_places_provider()
    return provider.shields_direct if provider is not None else False


def search_nearby_landmarks(latitude: float, longitude: float, radius: float, included_types: list[str]) -> list[dict[str, Any]]:
    """
    Search for landmark-type places near a coordinate (``controllers.maps``'s nearby-places endpoint).

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        radius: Search radius in meters.
        included_types: Google place-type taxonomy filter (e.g. ``["historical_landmark"]``).

    Returns:
        A list of Places API (New)-shaped dicts (``id``, ``displayName.text``, ``location.{latitude,longitude}``, ``shortFormattedAddress``, ``types``, ``rating``, ``userRatingCount``) - the exact shape the caller already parses.

    Raises:
        GatewayRequestError: The request to whichever provider answered failed - callers already wrap this call in their own exception handling."""
    return _require_provider().search_nearby_landmarks(latitude, longitude, radius, included_types)


# Deliberately one function per call site rather than a single unified "get place details" function:
# resolve_place_coordinates below asks for only the geometry and name because that's all the
# pin-add-autocomplete flow ever needs - merging it with this function's full field list would
# silently make every pin-add pay for Enterprise-tier fields it never uses, the exact SKU-tier cost
def get_place_details_full(place_id: str) -> dict[str, Any]:
    """
    Fetch full place details for the pin-detail panel, which needs the Places feature (``controllers.maps.place_details``).

    Args:
        place_id: The place id.

    Returns:
        A legacy-Google-Place-Details-shaped dict (``name``, ``formatted_address``, ``rating``, ``editorial_summary``, ``opening_hours``, ``website``, ``url``, ``photos``) - the exact shape the caller already parses/caches.

    Raises:
        GatewayRequestError: The request failed outright (network error, non-2xx) - deliberately left uncaught so the existing caller-level exception handling (which maps this to a 502 without caching it) keeps working for every provider."""
    return _require_provider().get_place_details_full(place_id)


def find_nearest_place_photos(latitude: float, longitude: float) -> tuple[str | None, list[str]]:
    """
    Find the nearest place's id and its photo identifiers (``plugins.builtin.google_places``'s Media-gallery panel).

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Tuple of (place_id, photo identifiers) - up to 10 identifiers, each an opaque string suitable for :func:`download_photo`.

    Raises:
        GatewayRequestError: The request failed outright - deliberately left uncaught (unlike most other functions in this module) so the panel-source framework's own failure-skip/retry machinery handles it."""
    return _require_provider().find_nearest_place_photos(latitude, longitude)


def download_photo(photo_identifier: str) -> tuple[bytes, str]:
    """
    Download one photo's actual image bytes (``controllers.media_proxy.GoogleMapsPhotoProxyView``).

    Args:
        photo_identifier: An opaque identifier from :func:`find_nearest_place_photos`.

    Returns:
        Tuple of (file bytes, content-type).

    Raises:
        PhotoNotFoundError: The provider confirmed this photo no longer exists.
        GatewayRequestError: A provider request failed outright.
        requests.exceptions.RequestException: The direct Google request failed.

    Note:
        Dispatches on the identifier's own provider, not on which provider is *currently* available - an identifier already encodes which provider produced it (it was persisted into ``LocationCache`` when first fetched), so a config change must not orphan already-cached identifiers."""
    for provider in _providers():
        if provider.owns_photo(photo_identifier):
            return provider.download_photo(photo_identifier)
    raise PhotoNotFoundError(f"No places provider recognises photo {photo_identifier!r}.")


def autocomplete_predictions(query: str) -> list[dict[str, str]]:
    """
    Predictions-as-you-type (``services.map_pins.autocomplete.search_places``).

    Args:
        query: User's partial search text.

    Returns:
        A list of normalized ``{"place_id", "main_text", "secondary_text"}`` dicts, in the provider's own ranked order.

    Raises:
        GatewayRequestError: The request failed outright - left uncaught so the request-path policy records a failure rather than caching an empty answer."""
    return _require_provider().autocomplete_predictions(query)


def resolve_place_coordinates(place_id: str) -> tuple[float | None, float | None, str | None]:
    """
    Look up coordinates for a place_id selected from autocomplete (``services.map_pins.autocomplete.resolve_place``).

    Args:
        place_id: ``place_id`` from an autocomplete prediction.

    Returns:
        (latitude, longitude, name) - all may be None on failure or when the place has no known coordinates.

    Raises:
        GatewayRequestError: The request failed outright - left uncaught so the request-path policy records a failure rather than caching a miss."""
    return _require_provider().resolve_place_coordinates(place_id)


def resolve_name_from_nearby(latitude: float, longitude: float, radius: float) -> str | None:
    """
    Resolve a place name from nearby-search results (``services.locations.google.GooglePlacesNameResolver``).

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        radius: Search radius in meters.

    Returns:
        The first meaningful nearby place name, or None (also when no provider is available or the request failed)."""
    provider = active_places_provider()
    if provider is None:
        return None
    try:
        return provider.resolve_name_from_nearby(latitude, longitude, radius)
    except GatewayRequestError as exc:
        # The caller (GooglePlacesNameResolver.resolve) only ever expected a narrow set of exceptions - a provider's
        # request failure must not propagate as an unhandled one layer up.
        logger.debug("Nearby search failed for name resolution at %s,%s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
        return None
