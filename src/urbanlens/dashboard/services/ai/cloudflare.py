from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Any, ClassVar

import requests
from typing_extensions import TypeVar

from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.message import MessageQueue

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "@cf/mistral/mistral-7b-instruct-v0.1"

Response = TypeVar("Response", bound=dict[str, Any], default=dict[str, Any])


class CloudflareGateway(LLMGateway[Response]):
    #: Cost per thousand (sent, received) tokens, in USD, per Cloudflare's
    #: published Workers AI per-model pricing (developers.cloudflare.com/workers-ai/platform/pricing).
    MODEL_COSTS: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        DEFAULT_MODEL: (Decimal("0.00011"), Decimal("0.00019")),
    }

    def setup(self, **kwargs):
        if not self.api_url:
            self.api_url = str(settings.cloudflare_worker_ai_endpoint)
        if not self.api_key:
            self.api_key = str(settings.cloudflare_ai_api_key)

        super().setup(**kwargs)

        if not self.api_url or not self.api_key:
            raise ValueError("Cloudflare AI Gateway requires an API URL and API Key.")

    def _lookup_model(self, model_name: str | None) -> str | None:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL

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
        message_input: dict[str, Any] = {"messages": message_queue.messages}
        url = f"{str(self.api_url).rstrip('/')}/{self.model.lstrip('/')}"

        try:
            logger.info("Sending cloudflare request")
            logger.debug("Cloudflare request: %s", message_input)
            response = requests.post(url, headers=headers, json=message_input, timeout=60)
        except requests.RequestException as e:
            logger.exception("Failed to send request to Cloudflare AI: %s", e)
            return None

        try:
            response.raise_for_status()
            return response.json()
        except requests.HTTPError:
            logger.exception("Cloudflare AI returned an HTTP error. Content: %s", response.text)
            return None
        except ValueError:
            logger.exception("Failed to parse Cloudflare AI response as json: %s", response.status_code)
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
        except KeyError:
            logger.exception("Failed to parse Cloudflare AI result.response: '%s'", response)
            return None

        return body
