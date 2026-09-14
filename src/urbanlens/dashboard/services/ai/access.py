"""Whether a profile may use AI at all, and whether it may use the interactive assistant.
Callers that need a cheap, side-effect-free yes/no before they spend anything get :func:`ai_features_enabled` instead."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


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
        Whether the assistant's own worker is deployed *and* this profile may use AI features generally."""
    from django.conf import settings

    if not getattr(settings, "UL_AI_WORKER_ENABLED", False):
        return False
    return ai_features_enabled(profile)
