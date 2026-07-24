from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, ClassVar

import anthropic
from anthropic.types import Message, MessageParam

from urbanlens.dashboard.models.site_settings.meta import DEFAULT_ANTHROPIC_MODEL
from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.message import MessageQueue

logger = logging.getLogger(__name__)

DEFAULT_MODEL = DEFAULT_ANTHROPIC_MODEL


class AnthropicGateway(LLMGateway[Message]):
    """AI gateway backed by Anthropic's Claude models.

    Unlike the other gateways, Claude reliably follows formatting and
    tool-protocol instructions (see UL-293's assistant loop), which is why
    it's the provider pinned for the AI chat assistant.
    """

    #: Cost per thousand (sent, received) tokens, in USD, per Anthropic's
    #: published pricing (platform.claude.com/docs/en/about-claude/pricing).
    MODEL_COSTS: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        "claude-haiku-4-5": (Decimal("0.001"), Decimal("0.005")),
        "claude-sonnet-5": (Decimal("0.003"), Decimal("0.015")),
        "claude-opus-4-8": (Decimal("0.005"), Decimal("0.025")),
    }

    def setup(self, **kwargs):
        if not self.api_key:
            self.api_key = settings.anthropic_api_key

        super().setup(**kwargs)

        if not self.api_key:
            raise ValueError("Anthropic AI Gateway requires an API key.")

    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL

    def get_client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key=self.api_key)

    def _get_response(self, message_queue: MessageQueue) -> Message | None:
        """
        Send the message queue to Anthropic's Messages API and return the response.

        Args:
            message_queue (MessageQueue):
                The message queue to send to Anthropic.

        Returns:
            Message | None:
                The response from Anthropic, or None if the request fails.

        """
        system_prompt = ""
        messages: list[MessageParam] = []
        for msg in message_queue.messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            elif msg["role"] == "assistant":
                messages.append(MessageParam(role="assistant", content=msg["content"]))
            else:
                messages.append(MessageParam(role="user", content=msg["content"]))

        try:
            client = self.get_client()
            return client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=messages,
            )
        except anthropic.APIError as e:
            logger.exception("Error sending a message to Anthropic: %s", e)
            return None

    def _parse_response(self, response: Message) -> str | None:
        """
        Parse the response from Anthropic and return the message body.

        Args:
            response (Message):
                The response from Anthropic.

        Returns:
            str | None:
                The parsed response text, or None if it couldn't be parsed.

        """
        try:
            body = "".join(block.text for block in response.content if block.type == "text")
        except AttributeError as e:
            logger.exception("Error retrieving response: %s", e)
            return None

        if not body:
            logger.error("Anthropic response contained no text content: %s", response)
            return None

        self.receive_tokens(body)
        logger.debug("AI Response: %s", body)
        return body
