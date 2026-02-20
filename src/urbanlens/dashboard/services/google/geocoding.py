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

import json
import logging
import re

import requests

from urbanlens.dashboard.models.cache import GeocodedLocation
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class GoogleGeocodingGateway(Gateway):
    def __init__(self, api_key: str | None = None):
        if not api_key:
            api_key = settings.google_maps_api_key

        self.api_key = api_key
        self.base_url = "https://maps.googleapis.com/maps/api/geocode/json"

        if not self.api_key:
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
        geocoded_location = GeocodedLocation.objects.filter(place_name=place_name).first()
        if geocoded_location:
            # parse json_response
            try:
                return json.loads(geocoded_location.json_response)
            except json.JSONDecodeError as e:
                logger.exception('Error decoding cached json_response for %s -> Message: "%s"', place_name, e)
                logger.exception("json_response: %s", geocoded_location.json_response)

                # Remove it from the cache
                geocoded_location.delete()
                import sys
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
        if not latitude or not longitude:
            raise ValueError("Latitude and longitude must be provided to retrieve_place_name.")
        
        # Check if the geocoded data for the given place name already exists in the database
        geocoded_location = GeocodedLocation.objects.filter(latitude=latitude, longitude=longitude).first()
        if geocoded_location:
            # parse json_response
            try:
                return json.loads(geocoded_location.json_response)
            except json.JSONDecodeError as e:
                logger.exception('Error decoding json_response for %s, %s -> Message: "%s"', latitude, longitude, e)
                logger.exception("json_response: %s", geocoded_location.json_response)
                # Remove it from the cache
                geocoded_location.delete()
                import sys
                sys.exit()
                return None
        
        params = {
            "latlng": f"{latitude},{longitude}",
            "key": self.api_key,
        }

        return self.get(params)
    
    def get(self, params: dict) -> dict | None:
        response = requests.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()
        return self.handle_response(response, params)
    
    def handle_response(self, response: requests.Response, request_data: dict | None = None) -> dict | None:
        """
        Handle a response from the Google Geocoding API
        """
        if not request_data:
            request_data = {}

        if getattr(response, "status_code", None) != 200 or getattr(response, "error_message", None) is not None:
            logger.error('Error getting place name for %s -> Message: "%s"', request_data, response.error_message)
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

        except Exception as e:
            logger.exception('Error parsing json response for %s -> Message: "%s"', request_data, e)
            return None

        try:
            # Cache it
            GeocodedLocation.objects.create(
                latitude=latitude,
                longitude=longitude,
                place_name=request_data.get("place_name", None),
                json_response=json.dumps(body),
            )
        except Exception as e:
            logger.exception('Error caching geocoded location for %s -> Message: "%s"', request_data, e)
        
        return body

    def get_place_name(self, latitude: float, longitude: float) -> str | None:

        if not latitude or not longitude:
            logger.error("Latitude and longitude must be provided to get_place_name.")
            return None
        
        body = self.geocode_coordinates(latitude, longitude)
        if not body:
            return None
        
        results = body.get("results", [])
        place_name: str | None = None
        if results:
            # Typically, the first result is the most relevant
            place_name = results[0].get("formatted_address")

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

    def extract_coordinates_from_url(self, url: str) -> tuple[float | None, float | None]:
        """
        Extracts latitude and longitude from a Google Maps URL.
        """
        # Grab coordinates from this format first: https://www.google.com/maps/search/42.960773,-74.250664
        matches = re.match(r".*maps/search/(?P<latitude>-?[0-9]+(\.[0-9]+)?),(?P<longitude>-?[0-9]+(\.[0-9]+)?)/?$", url)
        if matches:
            latitude = float(matches.group("latitude"))
            longitude = float(matches.group("longitude"))
            return latitude, longitude

        # Handle urls like: https://www.google.com/maps/place/CharlesTown+USA+Mall/data=!4m2!3m1!1s0x89d9464ca04ccc2b:0x1046b3e3426a2065
        # -- get the place name, then use the api to convert to coordinates
        matches = re.match(r".*maps/place/(?P<place_name>[^/]+)/data", url)

        if matches:
            place_name = matches.group("place_name")
            if not place_name:
                logger.error("Unable to extract place name from url: %s", url)
                return None, None
            
            place_name = self.decode_place_name(place_name)
            latitude, longitude = self.get_coordinates(place_name)
                
            if not latitude or not longitude:
                logger.error('Unable to geocode place name "%s" from url: %s', place_name, url)
                return None, None
            return latitude, longitude

        logger.error("Unable to extract coordinates from url: %s", url)
        return None, None
