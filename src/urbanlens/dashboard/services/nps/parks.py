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
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-17                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-17     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

import logging

import requests
from dataclasses import dataclass, field
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class NPSGateway(Gateway):
    api_key: str = settings.nps_api_key
    base_url: str = "https://developer.nps.gov/api/v1"
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        if not self.api_key:
            raise ValueError("NPS API key must be provided.")

    def get_park_images(self, park_code: str) -> list:
        """
        Retrieve images for a specific park using the NPS API
        """
        if not park_code:
            raise ValueError("Park code must be provided to retrieve images.")

        headers = {"X-Api-Key": self.api_key}
        endpoint = f"{self.base_url}/parks"
        params = {"parkCode": park_code}

        response = self.session.get(endpoint, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        return self.handle_response(response, params)

    def handle_response(self, response: requests.Response, request_data: dict | None = None) -> list:
        """
        Handle a response from the NPS API
        """
        if not request_data:
            request_data = {}

        if getattr(response, "status_code", None) != 200:
            logger.error('Error getting images for %s -> Status Code: "%s"', request_data, response.status_code)
            return []

        try:
            body = response.json()
            return body.get("data", [])[0].get("images", [])

        except Exception as e:
            logger.exception('Error parsing json response for %s -> Message: "%s"', request_data, e)
            return []
