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
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from dashboard.services.gateway import Gateway
import requests
from django.conf import settings
from dashboard.models.locations import Location

class SmithsonianGateway(Gateway):
    """
    Gateway for the Smithsonian Open Access API.
    """

    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.si.edu/openaccess/api/v1.0/search"

    def get_data(self, search_term):
        params = {
            "api_key": self.api_key,
            "q": search_term,
            "online_media_type": "Images"
        }
        response = requests.get(self.base_url, params=params)
        response.raise_for_status()  # Will raise an HTTPError for bad requests

        data = response.json()
        return self.parse_response(data)

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