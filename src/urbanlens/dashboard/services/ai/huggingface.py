from __future__ import annotations

import logging

from urbanlens.dashboard.services.ai.gateway import LLMGateway

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "tgi"


class HuggingFaceGateway(LLMGateway):
    """Intentionally incomplete stub for a future HuggingFace-backed provider."""

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
