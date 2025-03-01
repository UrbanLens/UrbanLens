"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    openai.py                                                                                          *
*        - Path:    /dashboard/services/ai/openai.py                                                                   *
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
from typing import Optional
import logging
import openai
from openai import OpenAI
from openai.types.chat import ChatCompletion
from UrbanLens.settings.app import settings
from UrbanLens.dashboard.services.ai.gateway import LLMGateway
from UrbanLens.dashboard.services.ai.message import MessageQueue

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gpt-3.5-turbo'

class OpenAIGateway(LLMGateway[ChatCompletion]):
    _api_key: str | None

    @property
    def api_key(self) -> str | None:
        return self._api_key
    
    @api_key.setter
    def api_key(self, value: Optional[str]):
        openai.api_key = value
        self._api_key = value

    def _lookup_model(self, model_name: Optional[str]) -> str:
        if not model_name:
            return DEFAULT_MODEL
        
        model_name = model_name.lower()
        return {"gpt-3.5": "gpt-3.5-turbo", "gpt-4": "gpt-4-1106-preview"}.get(model_name, model_name)
    
    def setup(self, **kwargs):
        if not self.api_key:
            self.api_key = settings.openai_api_key

        super().setup(**kwargs)

    def get_client(self) -> OpenAI:
        return OpenAI(
            base_url=str(self.api_url),
            api_key=self.api_key,
        )

    def _get_response(self, message_queue : MessageQueue) -> ChatCompletion | None:
        """
        Send a message to OpenAI and return the response.

            Args:
                message_queue (MessageQueue):
                    The queue of messages to send to OpenAI.

            Returns:
                ChatCompletion:
                    The response from OpenAI.
        """
        try:
            client = self.get_client()

            response = client.chat.completions.create(
                model = self.model,
                messages = message_queue.messages,
                max_tokens = self.max_tokens,
            )
        except openai.BadRequestError as e:
            logger.error(f"Error sending a message to OpenAI: {e}")
            return None

        return response

    def _parse_response(self, response: ChatCompletion) -> str | None:
        """
        Parse the response from OpenAI and return the message body.

            Args:
                response (ChatCompletion):
                    The response from OpenAI.

            Returns:
                str:
                    The parsed response from OpenAI.
        """
        try:
            body = response.choices[0].message.content
            self.receive_tokens(body)
            logger.debug("AI Response: %s", body)
        except Exception as e:
            logger.error(f"Error retrieving response: {e}")
            return None

        return body