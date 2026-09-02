from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.models.site_settings.meta import DEFAULT_ANTHROPIC_MODEL
from urbanlens.dashboard.services.ai.gateway import LLMGateway

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.inference_client import Provider

DEFAULT_MODEL = DEFAULT_ANTHROPIC_MODEL


class AnthropicGateway(LLMGateway):
    """AI gateway backed by Anthropic's Claude models.

    Unlike the other gateways, Claude reliably follows formatting and
    tool-protocol instructions (see UL-293's assistant loop), which is why
    it's the provider pinned for the AI chat assistant.
    """

    PROVIDER: ClassVar[Provider] = "anthropic"

    #: Cost per thousand (sent, received) tokens, in USD, per Anthropic's
    #: published pricing (platform.claude.com/docs/en/about-claude/pricing).
    MODEL_COSTS: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        "claude-haiku-4-5": (Decimal("0.001"), Decimal("0.005")),
        "claude-sonnet-5": (Decimal("0.003"), Decimal("0.015")),
        "claude-opus-4-8": (Decimal("0.005"), Decimal("0.025")),
    }

    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL
