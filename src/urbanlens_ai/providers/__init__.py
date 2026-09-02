"""Provider adapters: translate a normalized InferenceRequest to/from each SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens_ai.providers.base import ProviderAdapter, ProviderError
from urbanlens_ai.schema import Provider

if TYPE_CHECKING:
    from urbanlens_ai.config import InferenceConfig

__all__ = ["ProviderAdapter", "ProviderError", "build_adapter"]


def build_adapter(provider: Provider, config: InferenceConfig) -> ProviderAdapter:
    """Construct the adapter for ``provider``, using this service's own credentials.

    Args:
        provider: Which provider to build an adapter for.
        config: This service's configuration (holds the provider API keys).

    Returns:
        A ready-to-use adapter.

    Raises:
        ProviderError: No API key (or, for Cloudflare, endpoint) is configured
            for the requested provider.
    """
    if provider == "anthropic":
        from urbanlens_ai.providers.anthropic import AnthropicAdapter

        if not config.anthropic_api_key:
            raise ProviderError("No Anthropic API key configured")
        return AnthropicAdapter(config.anthropic_api_key)

    if provider == "openai":
        from urbanlens_ai.providers.openai import OpenAIAdapter

        if not config.openai_api_key:
            raise ProviderError("No OpenAI API key configured")
        return OpenAIAdapter(config.openai_api_key)

    from urbanlens_ai.providers.cloudflare import CloudflareAdapter

    if not config.cloudflare_ai_api_key or not config.cloudflare_worker_ai_endpoint:
        raise ProviderError("No Cloudflare Workers AI credentials configured")
    return CloudflareAdapter(config.cloudflare_ai_api_key, str(config.cloudflare_worker_ai_endpoint))
