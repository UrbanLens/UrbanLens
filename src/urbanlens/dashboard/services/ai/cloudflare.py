from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Any, ClassVar

import requests
from typing_extensions import TypeVar

from urbanlens.dashboard.models.site_settings.meta import DEFAULT_CLOUDFLARE_MODEL
from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.message import MessageQueue

logger = logging.getLogger(__name__)

# Single source of truth shared with SiteSettings.cloudflare_model's own
# default - these were previously two separate string literals that happened
# to match; keeping them as one import means a future change to the site's
# default Cloudflare model can't silently desync from MODEL_COSTS below,
# which would otherwise make every request quietly fall back to the generic
# default-cost estimate instead of this model's real published pricing.
DEFAULT_MODEL = DEFAULT_CLOUDFLARE_MODEL

Response = TypeVar("Response", bound=dict[str, Any], default=dict[str, Any])


class CloudflareGateway(LLMGateway[Response]):
    #: Cost per thousand (sent, received) tokens, in USD, per Cloudflare's
    #: published Workers AI per-model pricing (developers.cloudflare.com/workers-ai/platform/pricing,
    #: verified 2026-07-19). SiteSettings.cloudflare_model is free text (no
    #: dropdown constraint), so an admin can point it at any Workers AI model;
    #: only the default previously had a real entry here, so every other
    #: choice silently fell back to LLMGateway.DEFAULT_COST_PER_THOUSAND's
    #: generic estimate. This covers the other mainstream chat models most
    #: likely to actually get picked - not Cloudflare's entire catalog.
    MODEL_COSTS: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        DEFAULT_MODEL: (Decimal("0.00011"), Decimal("0.00019")),
        "@cf/meta/llama-3.1-8b-instruct": (Decimal("0.000282"), Decimal("0.000827")),
        "@cf/meta/llama-3.2-1b-instruct": (Decimal("0.000027"), Decimal("0.000201")),
        "@cf/meta/llama-3.2-3b-instruct": (Decimal("0.000051"), Decimal("0.000335")),
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast": (Decimal("0.000293"), Decimal("0.002253")),
        "@cf/google/gemma-3-12b-it": (Decimal("0.000345"), Decimal("0.000556")),
        "@cf/qwen/qwen3-30b-a3b-fp8": (Decimal("0.000051"), Decimal("0.000335")),
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
