from __future__ import annotations

import logging

from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "tgi"


class HuggingFaceGateway(LLMGateway):
    """Intentionally incomplete stub for a future HuggingFace-backed provider.

    Not implemented and not reachable: ``setup()`` always raises
    ``NotImplementedError`` below, and this class is not wired into
    ``services/ai/factory.py``'s provider dispatch or ``AiProviderChoice`` -
    there is no site setting or code path that can select "huggingface" as a
    provider today. The code below the raise is a rough sketch left over from
    starting this integration; it hasn't been reviewed or completed (missing
    the abstract ``_get_response``/``_parse_response`` implementations
    required by ``LLMGateway``, among other gaps - see ``cloudflare.py`` for
    the pattern a real implementation should follow). Don't spend time
    debugging why this provider can't be selected - it isn't finished yet.
    """

    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL

    def setup(self, **kwargs):
        raise NotImplementedError(
            "HuggingFaceGateway is not yet implemented. Implement abstractmethods, and generics, similar to cloudflare.py",
        )

        if not self.api_url:
            self.api_url = settings.huggingface_ai_endpoint
        if not self.api_key:
            self.api_key = settings.huggingface_ai_api_key

        super().setup(**kwargs)

        if not self.api_url or not self.api_key:
            raise ValueError("Cloudflare AI Gateway requires an API URL and API Key.")
