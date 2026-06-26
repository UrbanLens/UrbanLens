from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from django.contrib.auth.models import User

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
    except Exception:
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
        dict with ``show_dev_toolbar`` and ``dev_toolbar_theme_mode``.
    """
    show = False
    try:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        show = SiteSettings.get_current().show_dev_admin_features(request.user)
    except Exception:
        # TODO: Is this exception expected? If not, remove this. If yes, catch the specific exception type.
        logger.exception("Error adding dev toolbar")

    theme_mode = ""
    if show and isinstance(request.user, User):
        try:
            theme_mode = request.user.profile.theme_mode
        except Exception:
            # TODO: Is this exception expected? If not, remove this. If yes, catch the specific exception type.
            theme_mode = ""

    return {
        "show_dev_toolbar": show,
        "dev_toolbar_theme_mode": theme_mode,
    }


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

        return {"can_use_ai_features": user_has_feature(request.user, SiteFeature.AI)}
    except Exception:
        return {"can_use_ai_features": False}
