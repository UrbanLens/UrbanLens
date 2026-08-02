"""Resolves Google Places API calls to REData or direct Google, per call.

Single chokepoint for the "which provider answers a Places call" decision,
mirroring ``cid_resolution.py``'s precedent for the same REData-vs-Google
choice on CID resolution:

- REData configured (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` both set) -
  the primary deployment's path. REData owns Google Places API access and
  does its own cost optimization (permanent caching, Essentials/Pro-tier-only
  field selection - see ``google.redata_places_gateway``'s module docstring
  for exactly which fields that excludes: no rating, reviews, opening hours,
  price level, phone numbers, or website, ever, on this path).
- REData not configured - assumed to be an install without access to it.
  Falls back to calling Google Places directly via the existing
  ``google.places.GooglePlacesGateway``, preserving each call site's current
  field-cost footprint exactly (see each function's own docstring - a
  function that only ever asked Google for cheap fields must keep asking for
  only those same cheap fields on this fallback path, never more).

Each function below corresponds to exactly one pre-existing call site in this
codebase; there is deliberately no single unified "get place details"
function - see the module-level comment above ``get_place_details_full`` for
why merging would silently change what gets billed.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from urbanlens.dashboard.services.apis.locations.google.geocoding import LOCALITY_PLACE_TYPES
from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.security.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

PROVIDER_REDATA = "redata"
PROVIDER_GOOGLE = "google_places"

_REDATA_PHOTO_PREFIX = "redata:"

#: The exact legacy Google Place Details fields the VIP panel needs - kept as
#: a constant so both the REData and Google branches of
#: get_place_details_full stay obviously in sync with each other.
_VIP_DETAIL_FIELDS: tuple[str, ...] = ("name", "formatted_address", "rating", "editorial_summary", "opening_hours", "website", "url", "photos")


def _redata_configured() -> bool:
    return bool(settings.redata_api_url and settings.redata_api_key)


def search_nearby_landmarks(latitude: float, longitude: float, radius: float, included_types: list[str], *, api_key: str) -> list[dict[str, Any]]:
    """Search for landmark-type places near a coordinate (``controllers.maps``'s nearby-places endpoint).

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        radius: Search radius in meters.
        included_types: Google place-type taxonomy filter (e.g. ``["historical_landmark"]``).
        api_key: Google Places API key, used only on the Google fallback path.

    Returns:
        A list of Places API (New)-shaped dicts (``id``, ``displayName.text``,
        ``location.{latitude,longitude}``, ``shortFormattedAddress``,
        ``types``, ``rating``, ``userRatingCount``) - the exact shape the
        caller already parses. ``rating``/``userRatingCount`` are always
        ``None`` on the REData path (Enterprise-tier fields REData never
        fetches - see the module docstring).

    Raises:
        GatewayRequestError: The request to whichever provider answered
            failed - callers already wrap this call in their own
            exception handling (matching today's Google-only behavior).
    """
    if _redata_configured():
        results = RedataPlacesGateway().search_nearby(latitude, longitude, radius_meters=radius, included_types=included_types)
        return [
            {
                "id": r.get("place_id") or "",
                "displayName": {"text": r.get("name") or ""},
                "location": {"latitude": r.get("latitude"), "longitude": r.get("longitude")},
                "shortFormattedAddress": r.get("formatted_address") or "",
                "types": r.get("types") or [],
                "rating": None,
                "userRatingCount": None,
            }
            for r in results
        ]
    return GooglePlacesGateway(api_key=api_key).search_nearby(latitude, longitude, radius=radius, included_types=included_types)


# Deliberately one function per call site rather than a single unified
# "get place details" function: resolve_place_coordinates below asks Google
# for only ["geometry","name"] because that's all the pin-add-autocomplete
# flow ever needs - merging it with this function's full field list would
# silently make every pin-add pay for Enterprise-tier fields it never uses,
# the exact SKU-tier cost mistake this whole integration originated from.
def get_place_details_full(place_id: str, *, api_key: str) -> dict[str, Any]:
    """Fetch full place details for the VIP-only pin-detail panel (``controllers.maps.place_details``).

    Args:
        place_id: The Google Place id.
        api_key: Google Places API key, used only on the Google fallback path.

    Returns:
        A legacy-Google-Place-Details-shaped dict (``name``,
        ``formatted_address``, ``rating``, ``editorial_summary``,
        ``opening_hours``, ``website``, ``url``, ``photos``) - the exact
        shape the caller already parses/caches. On the REData path,
        ``rating``/``editorial_summary``/``opening_hours``/``website`` are
        always ``None`` (Enterprise-tier fields REData never fetches) and
        ``photos`` is always empty (nothing renders it today - see
        ``google.redata_places_gateway``). An empty dict means "REData
        confirmed no such place" - not a failure.

    Raises:
        GatewayRequestError: The request to whichever provider answered
            failed outright (network error, non-2xx) - deliberately left
            uncaught here so the existing caller-level exception handling
            (which maps this to a 502 without caching it) keeps working
            unchanged for both providers.
    """
    if _redata_configured():
        place = RedataPlacesGateway().get_place(place_id)
        if place is None:
            return {}
        return {
            "name": place.get("name") or "",
            "formatted_address": place.get("formatted_address") or "",
            "rating": None,
            "editorial_summary": None,
            "opening_hours": None,
            "website": None,
            "url": place.get("google_maps_uri") or "",
            "photos": [],
        }
    return GooglePlacesGateway(api_key=api_key).get_place_details(place_id, fields=list(_VIP_DETAIL_FIELDS))


def find_nearest_place_photos(latitude: float, longitude: float, *, api_key: str) -> tuple[str | None, list[str]]:
    """Find the nearest place's id and its photo identifiers (``plugins.builtin.google_places``'s Media-gallery panel).

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        api_key: Google Places API key, used only on the Google fallback path.

    Returns:
        Tuple of (place_id, photo identifiers) - up to 10 identifiers, each
        an opaque string suitable for :func:`download_photo`. On the Google
        path these are raw Places API (New) photo resource names
        (``places/.../photos/...``); on the REData path they're composite
        strings (``"redata:{place_id}:{record_id}"``) encoding REData's own
        internal photo-record id, since REData's photo download endpoint is
        keyed by that id, not Google's resource name. Either identifier form
        round-trips opaquely through the caller's HMAC-signed proxy URL
        scheme (``controllers.media_proxy``), which never inspects its
        internal structure.

    Raises:
        GatewayRequestError: The request to whichever provider answered
            failed outright - deliberately left uncaught here (unlike most
            other functions in this module) so the panel-source framework's
            own failure-skip/retry machinery handles it, matching
            ``plugins.builtin.property_records``'s established precedent for
            REData failures inside a panel-source fetch. Swallowing this to
            an empty result here would instead get permanently cached as
            "no photos found" on a merely transient REData outage.
    """
    if _redata_configured():
        gateway = RedataPlacesGateway()
        nearby = gateway.search_nearby(latitude, longitude, radius_meters=75, max_results=1)
        if not nearby:
            return None, []
        place_id = nearby[0].get("place_id") or None
        if place_id is None:
            return None, []
        place = gateway.get_place(place_id)
        if place is None:
            return place_id, []
        records = place.get("photo_records") or []
        identifiers = [f"{_REDATA_PHOTO_PREFIX}{place_id}:{record['id']}" for record in records if isinstance(record, dict) and record.get("id") is not None][:10]
        return place_id, identifiers

    google_gateway = GooglePlacesGateway(api_key=api_key)
    place_id = google_gateway.find_nearest_place_id(latitude, longitude)
    photo_names = google_gateway.get_place_photo_names(place_id, max_photos=10) if place_id else []
    return place_id, photo_names


class PhotoNotFoundError(Exception):
    """Confirmed no photo exists at this identifier, on whichever provider produced it.

    Lets ``controllers.media_proxy`` tell "gone" (cache as expired, 404) apart
    from "transient" (502, don't cache) - the same distinction it already
    makes for Google's own 404s.
    """


def download_photo(photo_identifier: str, *, api_key: str) -> tuple[bytes, str]:
    """Download one photo's actual image bytes (``controllers.media_proxy.GoogleMapsPhotoProxyView``).

    Args:
        photo_identifier: An opaque identifier from :func:`find_nearest_place_photos`.
        api_key: Google Places API key, used only when the identifier isn't
            REData-prefixed.

    Returns:
        Tuple of (file bytes, content-type).

    Raises:
        PhotoNotFoundError: REData confirmed this photo no longer exists.
        GatewayRequestError: A REData request failed outright (network error,
            non-2xx other than a confirmed-gone 404).
        requests.exceptions.RequestException: The direct Google request
            failed - unchanged from today's behavior, since
            ``GooglePlacesGateway.get_photo_media`` is called exactly as
            before for a non-REData identifier.

    Note:
        Dispatches on the identifier's own prefix, not on whether REData is
        *currently* configured - an identifier already encodes which
        provider produced it (it was persisted into ``LocationCache`` when
        first fetched), so a config change must not orphan already-cached
        identifiers.
    """
    if photo_identifier.startswith(_REDATA_PHOTO_PREFIX):
        _, place_id, record_id = photo_identifier.split(":", 2)
        result = RedataPlacesGateway().download_photo(place_id, int(record_id))
        if result is None:
            raise PhotoNotFoundError(f"REData has no photo at {photo_identifier!r}.")
        return result
    return GooglePlacesGateway(api_key=api_key).get_photo_media(photo_identifier)


def autocomplete_predictions(query: str, *, api_key: str) -> list[dict[str, str]]:
    """Predictions-as-you-type (``services.map_pins.autocomplete.search_google_places``).

    Args:
        query: User's partial search text.
        api_key: Google Places API key, used only on the Google fallback path.

    Returns:
        A list of normalized ``{"place_id", "main_text", "secondary_text"}``
        dicts, in either provider's own ranked order. A "query"-kind
        suggestion (no resolvable place) comes back with ``place_id: ""` -
        the caller's existing ``if not place_id: continue`` filter already
        discards those unchanged.

    Raises:
        GatewayRequestError: The request to whichever provider answered
            failed outright - deliberately left uncaught here so the
            existing caller-level ``except Exception`` (which returns
            whatever suggestions were already found, i.e. none) keeps
            working unchanged for both providers.
    """
    if _redata_configured():
        predictions = RedataPlacesGateway().autocomplete(query)
        return [{"place_id": p.get("place_id") or "", "main_text": p.get("main_text") or "", "secondary_text": p.get("secondary_text") or ""} for p in predictions]

    results: list[dict[str, str]] = []
    for pred in GooglePlacesGateway(api_key=api_key).autocomplete(query):
        fmt = pred.get("structured_formatting") or {}
        results.append(
            {
                "place_id": pred.get("place_id") or "",
                "main_text": fmt.get("main_text") or pred.get("description") or "",
                "secondary_text": fmt.get("secondary_text") or "",
            }
        )
    return results


def resolve_place_coordinates(place_id: str, *, api_key: str) -> tuple[float | None, float | None, str | None]:
    """Look up coordinates for a place_id selected from autocomplete (``services.map_pins.autocomplete.resolve_google_place``).

    Args:
        place_id: Google Places ``place_id`` from an autocomplete prediction.
        api_key: Google Places API key, used only on the Google fallback path.

    Returns:
        (latitude, longitude, name) - all may be None on failure or when the
        place has no known coordinates.

    Raises:
        GatewayRequestError: The request to whichever provider answered
            failed outright - deliberately left uncaught here so the
            existing caller-level ``except Exception`` (which falls back to
            ``(None, None, None)``) keeps working unchanged for both
            providers.
    """
    if _redata_configured():
        place = RedataPlacesGateway().get_place(place_id)
        if place is None:
            return None, None, None
        lat, lng, name = place.get("latitude"), place.get("longitude"), place.get("name")
        if lat is None or lng is None:
            return None, None, name
        return float(lat), float(lng), name

    details = GooglePlacesGateway(api_key=api_key).get_place_details(place_id, fields=["geometry", "name"])
    loc = details.get("geometry", {}).get("location", {})
    lat, lng = loc.get("lat"), loc.get("lng")
    name = details.get("name")
    if lat is not None and lng is not None:
        return float(lat), float(lng), name
    return None, None, name


def resolve_name_from_nearby(latitude: float, longitude: float, radius: float, *, api_key: str) -> str | None:
    """Resolve a place name from nearby-search results (``services.locations.google.GooglePlacesNameResolver``).

    Skips any result whose ``types`` are exclusively administrative/regional
    ones (see ``LOCALITY_PLACE_TYPES``) - a bare "locality" hit shouldn't name
    a pin after its city.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        radius: Search radius in meters.
        api_key: Google Places API key, used only on the Google fallback path.

    Returns:
        The first meaningful nearby place name, or None.
    """
    if _redata_configured():
        try:
            candidates = RedataPlacesGateway().search_nearby(latitude, longitude, radius_meters=radius)
        except GatewayRequestError as exc:
            # Unlike this module's other functions, the caller
            # (GooglePlacesNameResolver.resolve) only ever expected a narrow
            # set of Google-specific exceptions - GatewayRequestError isn't
            # one of them, so it must be swallowed here rather than left to
            # propagate into an unhandled exception one layer up.
            logger.debug("REData nearby search failed for name resolution at %s,%s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return None
    else:
        if not api_key:
            return None
        try:
            candidates = GooglePlacesGateway(api_key=api_key).get_data(latitude, longitude, radius=radius)
        except (OSError, ValueError, requests.RequestException, RequestCancelledError) as exc:
            logger.debug("Google Places name lookup failed for %s,%s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return None

    for result in candidates:
        types = set(result.get("types") or [])
        if types and not (types - LOCALITY_PLACE_TYPES):
            continue
        name = (result.get("name") or "").strip()
        if name:
            return name
    return None
