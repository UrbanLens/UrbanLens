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
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

import requests
import logging
from dashboard.services.gateway import Gateway
from dashboard.models.cache import GeocodedLocation

logger = logging.getLogger(__name__)

class GoogleGeocodingGateway(Gateway):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://maps.googleapis.com/maps/api/geocode/json"

    def get_place_name(self, latitude, longitude):

        if not latitude or not longitude:
            logger.error('Latitude and longitude must be provided to get_place_name.')
            return None

        # Check if the geocoded data for the given latitude and longitude already exists in the database
        geocoded_location = GeocodedLocation.objects.filter(latitude=latitude, longitude=longitude).first()
        if geocoded_location:
            # If it does, return the cached data
            return geocoded_location.place_name
        else:
            # If it doesn't, make a request to the Google Geocoding API
            params = {
                "latlng": f"{latitude},{longitude}",
                "key": self.api_key
            }
            response = requests.get(self.base_url, params=params)
            response.raise_for_status()

            results = response.json().get('results', [])
            place_name = None
            if results:
                # Typically, the first result is the most relevant
                place_name = results[0].get('formatted_address')

            # Save the geocoded data to the database
            GeocodedLocation.objects.create(
                latitude=latitude,
                longitude=longitude,
                place_name=place_name
            )

            return place_name
