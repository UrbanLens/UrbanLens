from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.ai.gateway import LLMGateway

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.inference_client import Provider

DEFAULT_MODEL = "gpt-5-nano"


class OpenAIGateway(LLMGateway):
    PROVIDER: ClassVar[Provider] = "openai"

    #: Cost per thousand (sent, received) tokens, in USD.
    MODEL_COSTS: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        "gpt-5.2": (Decimal("0.00175"), Decimal("0.014")),
        "gpt-5-mini": (Decimal("0.00025"), Decimal("0.002")),
        "gpt-5-nano": (Decimal("0.00005"), Decimal("0.0004")),
    }

    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL
