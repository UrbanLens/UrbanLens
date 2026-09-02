"""Cloudflare Workers AI provider adapter.

No tool-use translation: ``policy.validate_request`` refuses any request
that pairs ``provider="cloudflare"`` with a non-empty ``tools`` list before
this adapter is ever reached - Workers AI's tool-calling support is
model-dependent and unreliable enough that the assistant is pinned to
Anthropic (see ``dashboard.services.ai.assistant``); Cloudflare is the
low-cost text-only provider for everything else.
"""

from __future__ import annotations

import requests

from urbanlens_ai import policy
from urbanlens_ai.providers.base import ProviderAdapter, ProviderError
from urbanlens_ai.schema import InferenceRequest, InferenceResponse, TextBlock, Usage


class CloudflareAdapter(ProviderAdapter):
    """Translates :class:`InferenceRequest` to and from Cloudflare's Workers AI REST API."""

    def __init__(self, api_key: str, endpoint: str) -> None:
        policy.validate_cloudflare_endpoint(endpoint)
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")

    def send(self, request: InferenceRequest) -> InferenceResponse:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": message.role, "content": message.content} for message in request.messages)

        url = f"{self._endpoint}/{request.model.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            response = requests.post(url, headers=headers, json={"messages": messages}, timeout=policy.PROVIDER_TIMEOUT_SECONDS)
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise ProviderError(f"Cloudflare Workers AI call failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Cloudflare Workers AI returned unparseable JSON: {exc}") from exc

        try:
            text = body["result"]["response"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"Cloudflare Workers AI response missing result.response: {body!r}") from exc

        # Workers AI does not consistently report token usage across models;
        # the caller falls back to its own estimate when both fields are None.
        usage = Usage()
        if isinstance(body.get("result"), dict) and isinstance(body["result"].get("usage"), dict):
            raw_usage = body["result"]["usage"]
            usage = Usage(input_tokens=raw_usage.get("prompt_tokens"), output_tokens=raw_usage.get("completion_tokens"))

        return InferenceResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=usage)
