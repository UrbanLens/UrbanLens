from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import DatabaseError

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


def add_site_settings(request: HttpRequest) -> dict[str, str]:
    """Inject site-wide settings into every template context.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with site_title and app_version available in all templates.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    try:
        from urbanlens.dashboard.models.site_settings import SiteSettings
        site = SiteSettings.get_current()
        site_title = site.app_title
    except (ImportError, DatabaseError):
        site_title = "UrbanLens"

    return {
        "site_title": site_title,
        "app_version": app_settings.app_version,
    }


def add_dev_toolbar(request: HttpRequest) -> dict[str, bool | str]:
    """Inject dev toolbar visibility and theme state into template context.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``show_dev_toolbar``, ``dev_toolbar_theme_mode``, and
        ``dev_toolbar_map_dark_mode``.
    """
    show = False
    try:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        show = SiteSettings.get_current().show_dev_admin_features(request.user)
    except (ImportError, DatabaseError):
        logger.exception("Error adding dev toolbar")

    theme_mode = ""
    map_dark_mode = ""
    if show and isinstance(request.user, User):
        try:
            theme_mode = request.user.profile.theme_mode
            map_dark_mode = request.user.profile.map_dark_mode
        except AttributeError:
            theme_mode = ""
            map_dark_mode = ""

    return {
        "show_dev_toolbar": show,
        "dev_toolbar_theme_mode": theme_mode,
        "dev_toolbar_map_dark_mode": map_dark_mode,
    }


def add_environment_indicator(request: HttpRequest) -> dict[str, str]:
    """Expose the active environment to every template for the non-production indicator banner.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``env_indicator_type`` (lowercase environment value, e.g. ``"staging"``) and
        ``env_indicator_label`` (human-readable label). Both are empty strings in production,
        which templates use as the signal to hide the indicator.
    """
    from urbanlens.UrbanLens.environments.meta import EnvironmentTypes

    try:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        site = SiteSettings.get_current()
        env_type = site.get_effective_environment_type()
        if env_type == EnvironmentTypes.PRODUCTION:
            return {"env_indicator_type": "", "env_indicator_label": ""}
        return {
            "env_indicator_type": env_type.value,
            "env_indicator_label": site.get_effective_environment_label(),
        }
    except (ImportError, DatabaseError):
        return {"env_indicator_type": "", "env_indicator_label": ""}


def add_page_name(request: HttpRequest) -> dict[str, str]:
    resolver_match = request.resolver_match
    if resolver_match is None:
        return {"page_name": ""}
    page_name = resolver_match.url_name or ""
    # This will be a className, so replace anything that would trip up css
    page_name = re.sub(r"[^a-zA-Z0-9]", "-", page_name)
    return {"page_name": page_name}


def add_feature_access(request: HttpRequest) -> dict[str, bool]:
    """Expose subscription-gated feature visibility to templates."""
    try:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

        return {
            "can_use_ai_features": user_has_feature(request.user, SiteFeature.AI),
            "show_places_layer": user_has_feature(request.user, SiteFeature.PLACES),
            "can_use_web_search": user_has_feature(request.user, SiteFeature.SEARCH),
        }
    except (ImportError, DatabaseError):
        return {"can_use_ai_features": False, "show_places_layer": False, "can_use_web_search": False}
