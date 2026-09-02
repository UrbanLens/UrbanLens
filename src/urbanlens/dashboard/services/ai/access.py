"""Single chokepoint for whether the interactive AI assistant is available to a profile.

``services.ai.factory.get_gateway`` already enforces this same conjunction -
site-wide ``ai_enabled``, the profile's own ``ai_enabled``/``external_apis_enabled``
preferences, and the ``SiteFeature.AI`` subscription entitlement - for every
AI feature, but answering it there means constructing (and discarding) a
provider gateway instance. The interactive assistant needs a cheap,
side-effect-free yes/no it can call on every page load (the context
processor's ``assistant_enabled``) as well as the web view, the external API,
and the task itself before it spends a provider call - so it gets its own
named predicate rather than each of those re-deriving the conjunction or
paying for a throwaway gateway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def assistant_available(profile: Profile) -> bool:
    """Return whether ``profile`` may open and use the interactive AI assistant."""
    from django.conf import settings

    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if not getattr(settings, "UL_AI_WORKER_ENABLED", False):
        return False
    if not SiteSettings.get_current().ai_enabled:
        return False
    if not profile.ai_enabled or not profile.external_apis_enabled:
        return False
    return user_has_feature(profile.user, SiteFeature.AI)
