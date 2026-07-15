"""Ollama plugin: free, open-source, self-hosted vision-model photo keywords.

Unlike the OpenAI/Cloudflare vision keyword provider, this costs nothing per
call (the model runs on the admin's own hardware) and so needs no
subscription-feature gate - only that a server is actually configured and
the uploader's own keyword/AI toggles allow it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.photo_keywords import KeywordResult, PhotoKeywordProvider, downscaled_jpeg_bytes
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image


class OllamaVisionKeywordProvider(PhotoKeywordProvider):
    """Local vision-model photo keywords via a self-hosted Ollama server."""

    slug = "photo_keywords_ollama"
    label = "Photo keywords: Ollama (local vision)"

    def is_available_for(self, image: Image) -> bool:
        """Requires a configured Ollama server and the uploader's AI toggle.

        No subscription feature is required - the model runs locally at no
        per-call cost, unlike the OpenAI/Cloudflare vision provider.

        Args:
            image: The uploaded image.

        Returns:
            True when a local Ollama call is allowed for this uploader.
        """
        from urbanlens.UrbanLens.settings.app import settings

        profile = image.profile
        if profile is None or not profile.ai_enabled or not profile.external_apis_enabled:
            return False
        return bool(settings.ollama_base_url)

    def generate(self, image: Image) -> list[KeywordResult]:
        """Downscale the photo and ask the local Ollama vision model for keywords.

        Args:
            image: The uploaded image.

        Returns:
            Described keywords; empty when the call fails (errors logged).
        """
        from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway

        small = downscaled_jpeg_bytes(image)
        if small is None:
            return []
        return [KeywordResult(keyword=keyword) for keyword in OllamaGateway().describe_photo_keywords(small)]


class OllamaPlugin(UrbanLensPlugin):
    """Free, open-source, self-hosted vision-model photo keywords via Ollama."""

    name: ClassVar[str] = "ollama"
    verbose_name: ClassVar[str] = "Ollama (local vision AI)"
    description: ClassVar[str] = (
        "Free, open-source, self-hosted vision-model (e.g. LLaVA) photo keywording via a local Ollama "
        "server - no per-call cost, no subscription feature required. Requires UL_OLLAMA_BASE_URL to be "
        "configured and a running Ollama server with a vision model pulled."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for local Ollama calls."""
        return {
            "ollama": ServiceDefaults(
                display_name="Ollama (local vision AI)",
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Self-hosted, free - limits here are about not overloading the local server, not cost.",
            ),
        }

    def get_photo_keyword_providers(self) -> list[PhotoKeywordProvider]:
        """Contribute the Ollama vision keyword provider."""
        return [OllamaVisionKeywordProvider()]
