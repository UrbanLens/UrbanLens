from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.models.site_settings.meta import DEFAULT_CLOUDFLARE_MODEL
from urbanlens.dashboard.services.ai.gateway import LLMGateway

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.inference_client import Provider

# Single source of truth shared with SiteSettings.cloudflare_model's own
# default - these were previously two separate string literals that happened
# to match; keeping them as one import means a future change to the site's
# default Cloudflare model can't silently desync from MODEL_COSTS below,
# which would otherwise make every request quietly fall back to the generic
# default-cost estimate instead of this model's real published pricing.
DEFAULT_MODEL = DEFAULT_CLOUDFLARE_MODEL


class CloudflareGateway(LLMGateway):
    PROVIDER: ClassVar[Provider] = "cloudflare"

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

    def _lookup_model(self, model_name: str | None) -> str | None:
        if not model_name:
            return DEFAULT_MODEL

        if result := super()._lookup_model(model_name):
            return result

        return DEFAULT_MODEL
