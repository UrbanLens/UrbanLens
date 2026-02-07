"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    cloudflare.py                                                                                      *
*        - Path:    /dashboard/services/ai/cloudflare.py                                                               *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-03-21                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-21     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations
from typing import Any, Dict, TypeVar
import logging
import requests
from urbanlens.UrbanLens.settings.app import settings
from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.dashboard.services.ai.message import MessageQueue

logger = logging.getLogger(__name__)

DEFAULT_MODEL = '@cf/mistral/mistral-7b-instruct-v0.1'

Response = TypeVar("Response", bound=Dict[str, Any])

class CloudflareGateway(LLMGateway[Response]):
    
    def setup(self, **kwargs):
        if not self.api_url:
            self.api_url = settings.cloudflare_worker_ai_endpoint
        if not self.api_key:
            self.api_key = settings.cloudflare_ai_api_key

        super().setup(**kwargs)

        if not self.api_url or not self.api_key:
            raise ValueError("Cloudflare AI Gateway requires an API URL and API Key.")

    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL
        
        return super()._lookup_model(model_name)

    def _get_response(self, message_queue: MessageQueue) -> Response | None:
        """
        Send a request to the Cloudflare AI API and return the response.

            Args:
                message_queue (MessageQueue):
                    The message queue containing the messages to send to the model.

            Returns:
                Response | None:
                    The response from the Cloudflare AI API, or None if the request fails.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        input = { "messages": message_queue.messages }
        url = f"{self.api_url}{self.model}"

        try:
            logger.info('Cloudflare request: %s', input)
            response = requests.post(url, headers=headers, json=input, timeout=60)
        except requests.RequestException as e:
            logger.error("Failed to send request to Cloudflare AI: %s", e)
            return None
        
        try:
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as httpe:
            logger.error("Cloudflare AI returned an HTTP error. Content: %s -> %s", response.text, httpe)
            from icecream import ic
            ic(response)
            ic(response.text)
            return None
        except ValueError as ve:  
            logger.error("Failed to parse Cloudflare AI response as json: %s -> %s", response.status_code, ve)
            from icecream import ic
            ic(response)
            ic(response.text)
            return None
    
    def _parse_response(self, response: Response) -> str | None:
        """
        Parse the response from the Cloudflare AI API.

            Args:
                response (Response):
                    The response from the Cloudflare AI API.

            Returns:
                str | None:
                    The response body, or None if the response could not be parsed.
        """
        try:
            body = response["result"]["response"]
        except KeyError as e:
            logger.error("Failed to parse Cloudflare AI result.response: '%s' -> %s", response, e)
            return None
        
        return body