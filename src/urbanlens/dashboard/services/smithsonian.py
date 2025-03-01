"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    smithsonian.py                                                                                       *
*        Path:    /dashboard/services/smithsonian.py                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.1                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from django.core.cache import cache
from UrbanLens.dashboard.services.gateway import Gateway
import requests
from UrbanLens.settings.app import settings

class SmithsonianGateway(Gateway):
    """
    Gateway for the Smithsonian Open Access API.
    """

    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.si.edu/openaccess/api/v1.0/search"

    def get_data(self, search_term):
        # Create a unique cache key based on the search term
        cache_key = f'smithsonian_{search_term}'
        # Try to get the data from the cache
        data = cache.get(cache_key)
        # If the data is not in the cache
        if data is None:
            params = {
                "api_key": self.api_key,
                "q": search_term,
                "online_media_type": "Images"
            }
            response = requests.get(self.base_url, params=params)
            response.raise_for_status()  # Will raise an HTTPError for bad requests

            data = response.json()
            # Store the data in the cache for 24 hours (86400 seconds)
            cache.set(cache_key, data, 86400)
        return self.parse_response(data)

    def get_images_by_coordinates(self, latitude, longitude):
        from UrbanLens.dashboard.services.google.geocoding import GoogleGeocodingGateway

        # Get the place name from the coordinates
        google_gateway = GoogleGeocodingGateway(settings.google_maps_api_key)
        place_name = google_gateway.get_place_name(latitude, longitude)

        # Get the images from the Smithsonian API
        return self.get_data(place_name)

    def parse_response(self, data):
        images = []
        for record in data.get('response', {}).get('rows', []):
            image_data = {
                'title': record.get('title'),
                'url': record.get('content', {}).get('descriptiveNonRepeating', {}).get('online_media', {}).get('media', [{}])[0].get('content'),
                'thumbnail': record.get('content', {}).get('descriptiveNonRepeating', {}).get('online_media', {}).get('media', [{}])[0].get('thumbnail')
            }
            images.append(image_data)
        return images
