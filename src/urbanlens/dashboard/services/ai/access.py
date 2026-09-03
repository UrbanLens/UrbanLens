"""Whether a profile may use AI at all, and whether it may use the interactive assistant.

``services.ai.factory.get_gateway`` already enforces the first of those -
site-wide ``ai_enabled``, the profile's own ``ai_enabled``/``external_apis_enabled``
preferences, and the ``SiteFeature.AI`` subscription entitlement - for every
AI feature, but answering it there means constructing (and discarding) a
provider gateway instance. Callers that need a cheap, side-effect-free yes/no
before they spend anything get :func:`ai_features_enabled` instead.

The assistant adds one conjunct to that, and it is deliberately *not* folded
into the shared predicate: ``UL_AI_WORKER_ENABLED`` describes whether the
sandboxed ``ai-worker`` container is deployed, and only the interactive
assistant runs there. Every other LLM-backed feature (label styling,
auto-tagging, import assist) resolves an inference client through the shared
``ai-inference`` tier and is unaffected by that container's absence - so an
install that turns the chat assistant off to save resources keeps them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def ai_features_enabled(profile: Profile) -> bool:
    """Return whether ``profile`` may use any AI-backed feature.

    The conjunction ``get_gateway`` applies per call, minus the per-feature
    ``SiteSettings`` toggle: site-wide AI, the profile's two preferences, and
    the subscription entitlement.

    Args:
        profile: The profile the AI call would be made on behalf of.

    Returns:
        Whether an AI call for this profile is worth attempting at all.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if not SiteSettings.get_current().ai_enabled:
        return False
    if not profile.ai_enabled or not profile.external_apis_enabled:
        return False
    return user_has_feature(profile.user, SiteFeature.AI)


def assistant_available(profile: Profile) -> bool:
    """Return whether ``profile`` may open and use the interactive AI assistant.

    Args:
        profile: The profile asking for the assistant.

    Returns:
        Whether the assistant's own worker is deployed *and* this profile may
        use AI features generally.
    """
    from django.conf import settings

    if not getattr(settings, "UL_AI_WORKER_ENABLED", False):
        return False
    return ai_features_enabled(profile)
