from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Any, ClassVar

import openai
from openai import OpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.message import MessageQueue

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-nano"


class OpenAIGateway(LLMGateway[ChatCompletion]):
    _api_key: str | None

    #: Cost per thousand (sent, received) tokens, in USD.
    MODEL_COSTS: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        "gpt-5.2": (Decimal("0.00175"), Decimal("0.014")),
        "gpt-5-mini": (Decimal("0.00025"), Decimal("0.002")),
        "gpt-5-nano": (Decimal("0.00005"), Decimal("0.0004")),
    }

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @api_key.setter
    def api_key(self, value: str | None):
        openai.api_key = value
        self._api_key = value

    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL

    def setup(self, **kwargs):
        if not self.api_key:
            self.api_key = settings.openai_api_key

        super().setup(**kwargs)

    def get_client(self) -> OpenAI:
        return OpenAI(
            base_url=str(self.api_url),
            api_key=self.api_key,
        )

    def _get_response(self, message_queue: MessageQueue) -> ChatCompletion | None:
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

            messages: list[ChatCompletionMessageParam] = []
            for msg in message_queue.messages:
                role = msg["role"]
                content = msg["content"]
                if role == "system":
                    messages.append(ChatCompletionSystemMessageParam(role="system", content=content))
                elif role == "assistant":
                    messages.append(ChatCompletionAssistantMessageParam(role="assistant", content=content))
                else:
                    messages.append(ChatCompletionUserMessageParam(role="user", content=content))

            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
            )
        except openai.BadRequestError as e:
            logger.exception("Error sending a message to OpenAI: %s", e)
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
        except (IndexError, AttributeError) as e:
            logger.exception("Error retrieving response: %s", e)
            return None

        return body
