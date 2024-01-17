"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    gateway.py                                                                                           *
*        Path:    /dashboard/services/nps/map.py                                                                       *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-17                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-17     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import requests
import logging
from django.conf import settings
from dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

class NPSGateway(Gateway):
    def __init__(self, api_key : str | None = None):
        if not api_key:
            api_key = settings.NPS_API_KEY
        self.api_key = api_key
        self.base_url = "https://developer.nps.gov/api/v1"

    def get_park_images(self, park_code: str) -> list:
        """
        Retrieve images for a specific park using the NPS API
        """
        if not park_code:
            raise ValueError('Park code must be provided to retrieve images.')
        
        headers = {"X-Api-Key": self.api_key}
        endpoint = f"{self.base_url}/parks"
        params = {"parkCode": park_code}

        response = requests.get(endpoint, headers=headers, params=params)
        response.raise_for_status()
        return self.handle_response(response, params)
    
    def handle_response(self, response: requests.Response, request_data: dict | None = None) -> list:
        """
        Handle a response from the NPS API
        """
        if not request_data:
            request_data = {}

        if getattr(response, 'status_code', None) != 200:
            logger.error('Error getting images for %s -> Status Code: "%s"', request_data, response.status_code)
            return []

        try:
            body = response.json()
            images = body.get('data', [])[0].get('images', [])
            return images

        except Exception as e:
            logger.error('Error parsing json response for %s -> Message: "%s"', request_data, e)
            return []
