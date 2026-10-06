"""Whether a profile may use AI at all, and whether it may use the interactive assistant.
Callers that need a cheap, side-effect-free yes/no before they spend anything get :func:`ai_features_enabled` instead."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


#: What the assistant says, in place of a reply, where hosted AI is not called (D26).
ASSISTANT_UNAVAILABLE_HERE_REPLY = "The assistant is not available in this environment."


def ai_refused_here(feature: str) -> bool:
    """Whether this environment makes no hosted AI call for ``feature`` (D26).

    Development and local refuse every hosted AI provider unless ``UL_ENVIRONMENT_SHARE_OVERRIDES`` names the feature
    or a provider. Callers say "not available in this environment" rather than "turned off": no setting of the
    account's or the site's changes it.

    Args:
        feature: The AI feature's service key, e.g. ``"link_extraction"``.

    Returns:
        True when a call for ``feature`` would be refused before it was made.
    """
    from urbanlens.dashboard.services.core.egress import egress_permitted

    return not egress_permitted(feature)


def ai_features_enabled(profile: Profile) -> bool:
    """Return whether ``profile`` may use any AI-backed feature.

    Args:
        profile: The profile the AI call would be made on behalf of.

    Returns:
        Whether an AI call for this profile is worth attempting at all."""
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
        Whether the assistant's own worker is deployed, this environment calls the assistant's provider, *and* this
        profile may use AI features generally."""
    from django.conf import settings

    if not getattr(settings, "UL_AI_WORKER_ENABLED", False):
        return False
    if ai_refused_here("assistant"):
        return False
    return ai_features_enabled(profile)
