"""AI gateway factory — selects provider and checks feature flags from SiteSettings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.services.ai.gateway import LLMGateway

logger = logging.getLogger(__name__)

# Maps feature keys to the SiteSettings field that controls them.
_FEATURE_FIELDS: dict[str, str] = {
    "category_suggestions": "ai_category_suggestions_enabled",
}


def get_gateway(feature: str | None = None, **kwargs) -> LLMGateway | None:
    """Return a configured AI gateway, or None if AI is disabled.

    Reads provider, model, and feature-flag state from SiteSettings so the
    site admin can control AI behaviour without a code deploy.

    Args:
        feature: Optional feature key (see ``_FEATURE_FIELDS``).  When provided,
            that feature's per-toggle must be enabled in addition to the global
            ``ai_enabled`` flag.
        **kwargs: Extra keyword arguments forwarded to the gateway constructor
            (e.g. ``instructions``, ``formatting``).

    Returns:
        A configured ``LLMGateway`` subclass instance, or ``None`` if AI is
        globally disabled or the requested feature is turned off.
    """
    from urbanlens.dashboard.models.trips.model import SiteSettings

    site = SiteSettings.get_current()

    if not site.ai_enabled:
        logger.debug("AI is globally disabled; skipping AI call")
        return None

    if feature:
        field = _FEATURE_FIELDS.get(feature)
        if field and not getattr(site, field, True):
            logger.debug("AI feature '%s' is disabled; skipping AI call", feature)
            return None

    provider = site.ai_provider

    if provider == "openai":
        from urbanlens.dashboard.services.ai.openai import OpenAIGateway

        return OpenAIGateway(model=site.openai_model or None, **kwargs)

    if provider == "cloudflare":
        from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway

        return CloudflareGateway(model=site.cloudflare_model or None, **kwargs)

    logger.warning("Unknown AI provider '%s'; falling back to Cloudflare", provider)
    from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway

    return CloudflareGateway(**kwargs)
