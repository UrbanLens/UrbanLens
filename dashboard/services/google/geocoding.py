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
from dashboard.services.gateway import Gateway

class GoogleGeocodingGateway(Gateway):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://maps.googleapis.com/maps/api/geocode/json"

    def get_place_name(self, latitude, longitude):
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
        from dashboard.models.locations.model import GeocodedLocation
        GeocodedLocation.objects.create(
            latitude=latitude,
            longitude=longitude,
            place_name=place_name
        )

        return place_name
