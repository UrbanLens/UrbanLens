from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar

from django.db import DatabaseError
import s2sphere

from urbanlens.dashboard.models.cache import GeocodedLocation
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate, redact_params
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from decimal import Decimal

    import requests

logger = logging.getLogger(__name__)

# Google place/geocoding "types" that identify an administrative area or other
# coarse region rather than a specific point of interest. A geocoding result
# carrying only these types names the *surroundings* (a city, neighborhood,
# postal code, ...), not the pinned place, so it must not be handed back as a
# usable place name (see ``get_place_name`` below and its sibling filter on
# ``GooglePlacesNameResolver`` in ``services.locations.google``).
LOCALITY_PLACE_TYPES: frozenset[str] = frozenset(
    {
        "locality",
        "sublocality",
        "sublocality_level_1",
        "sublocality_level_2",
        "sublocality_level_3",
        "sublocality_level_4",
        "sublocality_level_5",
        "neighborhood",
        "postal_town",
        "colloquial_area",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
        "administrative_area_level_4",
        "administrative_area_level_5",
        "administrative_area_level_6",
        "administrative_area_level_7",
        "country",
        "postal_code",
        "political",
        "plus_code",
    },
)


def parse_address_components(address_components: list[dict[str, Any]]) -> dict[str, str]:
    """Flatten a geocoding result's ``address_components`` into a type -> value map.

    Most fields prefer ``short_name`` (e.g. state abbreviations like "CA"), but
    ``country`` is stored under its own key using ``long_name`` (e.g. "Germany")
    since the ISO short code (e.g. "DE") isn't a useful display value.

    Args:
        address_components: The ``address_components`` list from a single
            Google Geocoding API result.

    Returns:
        Mapping of Google address component type (e.g. ``"locality"``,
        ``"country"``) to its parsed value.
    """
    type_map: dict[str, str] = {}
    country_name = ""
    for comp in address_components:
        types = comp.get("types", [])
        for t in types:
            type_map.setdefault(t, comp.get("short_name") or comp.get("long_name") or "")
        if "country" in types and not country_name:
            country_name = comp.get("long_name") or comp.get("short_name") or ""
    if country_name:
        type_map["country"] = country_name
    return type_map


@dataclass(slots=True, kw_only=True)
class GoogleGeocodingGateway(Gateway):
    service_key: ClassVar[str] = "google_geocoding"
    paid_service: ClassVar[bool] = True

    api_key: str | None = field(default_factory=lambda: settings.google_unrestricted_api_key)
    base_url: str = "https://maps.googleapis.com/maps/api/geocode/json"

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.api_key:
            # Deliberately does not raise - most callers (e.g. pin import's CSV/URL
            # parsing) construct this gateway before knowing whether any row will
            # actually need a live Google lookup (S2-cell CID decoding and plain
            # lat/lon columns never do). The network-calling methods below each
            # check self.api_key themselves and degrade gracefully instead.
            logger.debug("GoogleGeocodingGateway constructed with no API key configured - geocoding calls will be skipped.")

    def decode_place_name(self, place_name: str) -> str:
        """
        When a place name is retrieved from a url, decode it into plaintext
        """
        return place_name.replace("+", " ")

    def geocode_place_name(self, place_name: str) -> dict | None:
        """
        Retrieve a place name from the Google Geocoding API
        """
        if not place_name:
            raise ValueError("Place name must be provided to retrieve_place_name.")

        # Check if the geocoded data for the given place name already exists in the database
        geocoded_location: GeocodedLocation = GeocodedLocation.objects.all().filter(place_name=place_name).first()
        if geocoded_location:
            # parse json_response
            try:
                return json.loads(geocoded_location.json_response or "null")
            except json.JSONDecodeError as e:
                logger.exception('Error decoding cached json_response for %s -> Message: "%s"', place_name, e)
                logger.exception("json_response: %s", geocoded_location.json_response)

                # Remove it from the cache
                geocoded_location.delete()
                return None

        if not self.api_key:
            logger.debug("Skipping Google geocoding for %r - no API key configured.", place_name)
            return None

        params = {
            "address": place_name,
            "key": self.api_key,
        }

        return self.get(params)

    def geocode_coordinates(self, latitude: float, longitude: float) -> dict | None:
        """
        Use Google Geocoding API to retrieve a data about a location given its coordinates
        """
        if latitude is None or longitude is None:
            raise ValueError("Latitude and longitude must be provided to retrieve_place_name.")

        # Check if the geocoded data for the given place name already exists in the database
        geocoded_location: GeocodedLocation = GeocodedLocation.objects.all().filter(latitude=latitude, longitude=longitude).first()
        if geocoded_location:
            # parse json_response
            try:
                return json.loads(geocoded_location.json_response or "null")
            except json.JSONDecodeError as e:
                logger.exception(
                    'Error decoding json_response for (type: %s, %s -> %s, %s) -> Message: "%s"',
                    type(latitude),
                    type(longitude),
                    redact_coordinate(latitude),
                    redact_coordinate(longitude),
                    e,
                )
                logger.exception("json_response: %s", geocoded_location.json_response)
                # Remove it from the cache
                geocoded_location.delete()
                return None

        if not self.api_key:
            logger.debug("Skipping Google geocoding for %s, %s - no API key configured.", redact_coordinate(latitude), redact_coordinate(longitude))
            return None

        params = {
            "latlng": f"{latitude},{longitude}",
            "key": self.api_key,
        }

        return self.get(params)

    def get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        response = self.session.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()
        return self.handle_response(response, params)

    def handle_response(self, response: requests.Response, request_data: dict | None = None) -> dict | None:
        """
        Handle a response from the Google Geocoding API
        """
        if not request_data:
            request_data = {}

        if getattr(response, "status_code", None) != 200 or getattr(response, "error_message", None) is not None:
            logger.error(
                'Error getting place name for %s -> Message: "%s"',
                redact_params(request_data),
                getattr(response, "error_message", None),
            )
            return None

        try:
            body = response.json()
            results = body.get("results", [])
            latitude = None
            longitude = None
            if results:
                # Typically, the first result is the most relevant
                latitude = results[0].get("geometry", {}).get("location", {}).get("lat")
                longitude = results[0].get("geometry", {}).get("location", {}).get("lng")

        except (json.JSONDecodeError, KeyError):
            logger.exception("Error parsing json response for %s", redact_params(request_data))
            logger.info("json response type: %s", type(response))
            return None

        try:
            # Cache it
            GeocodedLocation.objects.create(
                latitude=latitude,
                longitude=longitude,
                place_name=request_data.get("place_name"),
                json_response=json.dumps(body),
            )
        except DatabaseError:
            logger.exception("Error caching geocoded location for %s", redact_params(request_data))

        return body

    def get_place_name(self, latitude: float | Decimal, longitude: float | Decimal) -> str | None:
        """Return the formatted address of the most relevant non-administrative geocoding result.

        Results whose ``types`` are entirely administrative/regional (a bare
        "locality" hit for a rural pin with no closer address, an
        "administrative_area_level_*", a "postal_code", ...) name the
        surrounding area rather than the pinned place, so they are skipped in
        favor of the first result with at least one finer-grained type (e.g.
        "street_address", "premise", "establishment").

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            The winning result's formatted address, or None when geocoding
            failed or every result was purely administrative.
        """
        if latitude is None or longitude is None:
            logger.error("Latitude and longitude must be provided to get_place_name.")
            return None

        latitude = float(latitude)
        longitude = float(longitude)

        try:
            body = self.geocode_coordinates(latitude, longitude)
            if not body:
                return None

            results = body.get("results", [])
            place_name: str | None = None
            for result in results:
                types = set(result.get("types") or [])
                if types and not (types - LOCALITY_PLACE_TYPES):
                    continue
                place_name = result.get("formatted_address")
                if place_name:
                    break
        except KeyError:
            logger.exception(
                "Error getting place name for latitude: %s, longitude: %s",
                redact_coordinate(latitude),
                redact_coordinate(longitude),
            )
            return None

        return place_name

    def get_coordinates(self, place_name: str) -> tuple[float | None, float | None]:
        """
        Retrieve coordinates from the Google Geocoding API
        """
        if not place_name:
            logger.error("Place name must be provided to get_coordinates.")
            return None, None

        body = self.geocode_place_name(place_name)
        if not body:
            return None, None

        results = body.get("results", [])
        latitude = None
        longitude = None
        if results:
            # Typically, the first result is the most relevant
            latitude = results[0].get("geometry", {}).get("location", {}).get("lat")
            longitude = results[0].get("geometry", {}).get("location", {}).get("lng")

        return latitude, longitude

    def get_coordinates_by_cid(self, cid: int) -> tuple[float | None, float | None]:
        """Look up coordinates by Google Maps CID via the Places Details API.

        CIDs are extracted from the ``!1s0x...`` segment of Google Maps place URLs.
        Results are cached in :class:`GeocodedLocation` under the key ``cid:{cid}``.

        Args:
            cid: Decimal CID value derived from the hex identifier in the URL.

        Returns:
            Tuple of (latitude, longitude), or (None, None) if the lookup fails.
        """
        cache_key = f"cid:{cid}"
        cached = GeocodedLocation.objects.filter(place_name=cache_key).first()
        if cached:
            try:
                body = json.loads(cached.json_response or "null")
                if body:
                    loc = body.get("result", {}).get("geometry", {}).get("location", {})
                    lat = loc.get("lat")
                    lng = loc.get("lng")
                    if lat is not None and lng is not None:
                        return float(lat), float(lng)
            except (json.JSONDecodeError, TypeError):
                cached.delete()

        if not self.api_key:
            logger.debug("Skipping Places Details lookup for CID %d - no API key configured.", cid)
            return None, None

        params = {"cid": str(cid), "fields": "geometry", "key": self.api_key}
        response = self.session.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()

        if body.get("status") != "OK":
            status = body.get("status")
            # REQUEST_DENIED is expected for residential addresses and locations
            # without a Google Places listing - not a key/config problem.
            logger.warning(
                "Places Details API returned status %s for CID %d",
                status,
                cid,
            )
            return None, None

        loc = body.get("result", {}).get("geometry", {}).get("location", {})
        lat = loc.get("lat")
        lng = loc.get("lng")

        try:
            GeocodedLocation.objects.create(
                place_name=cache_key,
                latitude=lat,
                longitude=lng,
                json_response=json.dumps(body),
            )
        except DatabaseError:
            logger.warning("Failed to cache CID lookup for %d", cid)

        if lat is None or lng is None:
            return None, None
        return float(lat), float(lng)

    def _decode_s2_cell(self, s2_hex: str) -> tuple[float | None, float | None]:
        """Decode an S2 cell ID hex string to (latitude, longitude).

        Args:
            s2_hex: Hex string of the S2 cell ID (without 0x prefix).

        Returns:
            Tuple of (latitude, longitude), or (None, None) if decoding fails.
        """
        cell = s2sphere.CellId(int(s2_hex, 16))
        if not cell.is_valid():
            return None, None
        ll = cell.to_lat_lng()
        lat = ll.lat().degrees
        lon = ll.lng().degrees
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
        return None, None

    def extract_coordinates_from_url(self, url: str) -> tuple[float | None, float | None]:
        """Extract latitude and longitude from a Google Maps URL.

        Handles:

        - ``/maps/search/{lat},{lon}`` - direct coordinates, no API call needed.
        - ``/maps/place/{name}/data=...`` - S2 cell decoded from the ``!1s0x{CELL}:0x{CID}``
          segment (precise, no API call, works for all locations including residential
          addresses). Falls back to CID lookup, then geocoding by place name.
        - ``/maps/place/{name}`` - geocoding by place name only.

        Args:
            url: Google Maps URL to parse.

        Returns:
            Tuple of (latitude, longitude), or (None, None) when extraction fails.
        """
        # Direct coordinates: .../maps/search/42.960773,-74.250664
        m = re.search(
            r"maps/search/(?P<lat>-?[0-9]+\.[0-9]+),(?P<lon>-?[0-9]+\.[0-9]+)",
            url,
        )
        if m:
            return float(m.group("lat")), float(m.group("lon"))

        # Place URL: .../maps/place/{name}[/data={encoded}]
        # name can be empty when Google omits the place name from the URL.
        m = re.search(
            r"maps/place/(?P<name>[^/?#]*)(?:/data=(?P<data>[^?#]*))?",
            url,
        )
        if m:
            place_name = self.decode_place_name(m.group("name"))
            data_param = m.group("data") or ""

            # The data segment encodes !1s0x{S2_CELL}:0x{PLACE_CID}.
            # The S2 cell (first hex value) directly encodes the pin location -
            # no API call needed, works for every URL including residential addresses.
            feature_match = re.search(r"!1s0x([0-9a-fA-F]+):0x([0-9a-fA-F]+)", data_param)
            if feature_match:
                s2_hex = feature_match.group(1)
                cid = int(feature_match.group(2), 16)

                try:
                    lat, lon = self._decode_s2_cell(s2_hex)
                    if lat is not None and lon is not None:
                        return lat, lon
                except (ValueError, OSError) as exc:
                    logger.warning("S2 cell decode failed for %s: %s", url, exc)

                try:
                    lat, lon = self.get_coordinates_by_cid(cid)
                    if lat is not None and lon is not None:
                        return lat, lon
                except (ValueError, OSError) as exc:
                    logger.warning("CID lookup failed for %s: %s", url, exc)

            # Fall back to geocoding by place name.
            lat, lon = self.get_coordinates(place_name)
            if lat is not None and lon is not None:
                return lat, lon

            logger.warning('Unable to resolve place "%s" from url: %s', place_name, url)
            return None, None

        logger.warning("Unrecognised Google Maps URL format: %s", url)
        return None, None
