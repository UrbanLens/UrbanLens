"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    geocoding.py                                                                                         *
*        Path:    /dashboard/services/google/geocoding.py                                                              *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

import s2sphere

from urbanlens.dashboard.models.cache import GeocodedLocation
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from decimal import Decimal

    import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleGeocodingGateway(Gateway):
    api_key: str | None = settings.google_maps_api_key
    base_url: str = "https://maps.googleapis.com/maps/api/geocode/json"

    def __post_init__(self) -> None:
        if not self.api_key:
            # TODO: Build k=>v pairs for all settings to help logging (temporarily for debugging)
            settings_dict = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
            logger.error("Settings that are missing the api key: %s", settings_dict)
            raise ValueError("Google Geocoding Gateway requires an API Key.")

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
                sys.exit()
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
        geocoded_location: GeocodedLocation = (
            GeocodedLocation.objects.all().filter(latitude=latitude, longitude=longitude).first()
        )
        if geocoded_location:
            # parse json_response
            try:
                return json.loads(geocoded_location.json_response or "null")
            except json.JSONDecodeError as e:
                logger.exception('Error decoding json_response for %s, %s -> Message: "%s"', latitude, longitude, e)
                logger.exception("json_response: %s", geocoded_location.json_response)
                # Remove it from the cache
                geocoded_location.delete()
                sys.exit()
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
                request_data,
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
            logger.exception("Error parsing json response for %s", request_data)
            return None

        try:
            # Cache it
            GeocodedLocation.objects.create(
                latitude=latitude,
                longitude=longitude,
                place_name=request_data.get("place_name", None),
                json_response=json.dumps(body),
            )
        except Exception:
            logger.exception("Error caching geocoded location for %s", request_data)

        return body

    def get_place_name(self, latitude: float | Decimal, longitude: float | Decimal) -> str | None:
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
            if results:
                # Typically, the first result is the most relevant
                place_name = results[0].get("formatted_address")
        except KeyError:
            logger.exception(
                "Error getting place name for latitude: %s, longitude: %s",
                latitude,
                longitude,
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
            # without a Google Places listing — not a key/config problem.
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
        except Exception:
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

        - ``/maps/search/{lat},{lon}`` — direct coordinates, no API call needed.
        - ``/maps/place/{name}/data=...`` — S2 cell decoded from the ``!1s0x{CELL}:0x{CID}``
          segment (precise, no API call, works for all locations including residential
          addresses). Falls back to CID lookup, then geocoding by place name.
        - ``/maps/place/{name}`` — geocoding by place name only.

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
            # The S2 cell (first hex value) directly encodes the pin location —
            # no API call needed, works for every URL including residential addresses.
            feature_match = re.search(r"!1s0x([0-9a-fA-F]+):0x([0-9a-fA-F]+)", data_param)
            if feature_match:
                s2_hex = feature_match.group(1)
                cid = int(feature_match.group(2), 16)

                try:
                    lat, lon = self._decode_s2_cell(s2_hex)
                    if lat is not None and lon is not None:
                        return lat, lon
                except Exception as exc:
                    logger.warning("S2 cell decode failed for %s: %s", url, exc)

                try:
                    lat, lon = self.get_coordinates_by_cid(cid)
                    if lat is not None and lon is not None:
                        return lat, lon
                except Exception as exc:
                    logger.warning("CID lookup failed for %s: %s", url, exc)

            # Fall back to geocoding by place name.
            lat, lon = self.get_coordinates(place_name)
            if lat is not None and lon is not None:
                return lat, lon

            logger.warning('Unable to resolve place "%s" from url: %s', place_name, url)
            return None, None

        logger.warning("Unrecognised Google Maps URL format: %s", url)
        return None, None
