from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import DatabaseError

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


def add_site_settings(request: HttpRequest) -> dict[str, str | bool]:
    """Inject site-wide settings into template context.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with site_title, app_version, public_costs_page_enabled.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    try:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        site = SiteSettings.get_current()
        site_title = site.app_title
        public_costs_page_enabled = site.public_costs_page_enabled
    except (ImportError, DatabaseError):
        site_title = "UrbanLens"
        public_costs_page_enabled = False

    return {
        "site_title": site_title,
        "app_version": app_settings.app_version,
        "public_costs_page_enabled": public_costs_page_enabled,
    }


def add_dev_toolbar(request: HttpRequest) -> dict[str, bool | str]:
    """Inject dev toolbar visibility and theme state.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with show_dev_toolbar, theme and map dark modes.
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
    """Expose the active environment for the non-production banner.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with env_indicator_type and env_indicator_label (empty in prod).
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


def add_demo_context(request: HttpRequest) -> dict[str, object]:
    """Expose demo flags to templates.

    ``demo_url`` lives on the real site (button target); ``demo_mode`` on the
    demo instance itself (banner). Never both.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``demo_url`` and ``demo_mode``.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return {
        "demo_url": app_settings.demo_url,
        "demo_mode": app_settings.demo_mode,
        # Banner link to the real site; empty hides it.
        "demo_signup_url": app_settings.demo_real_site_url,
    }


#: Nav aliases for sections reached from another page (e.g. pin.* keeps Map active).
_NAV_SECTION_ALIASES = {"pin": "map", "spotguessr": "games", "trivia": "games"}


def add_page_name(request: HttpRequest) -> dict[str, str]:
    """Expose the current page and nav section to templates.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``page_name`` (CSS-safe URL name) and ``nav_section``.
    """
    resolver_match = request.resolver_match
    if resolver_match is None:
        return {"page_name": "", "nav_section": ""}
    url_name = resolver_match.url_name or ""
    page_name = re.sub(r"[^a-zA-Z0-9]", "-", url_name)
    section = url_name.split(".", 1)[0] if url_name else ""
    nav_section = _NAV_SECTION_ALIASES.get(section, section)
    return {"page_name": page_name, "nav_section": nav_section}


def add_distance_units(request: HttpRequest) -> dict[str, str]:
    """Expose the viewer's distance unit to templates.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``distance_units`` ("km"/"mi"), defaulting to "km".
    """
    from urbanlens.dashboard.models.profile.meta import DistanceUnit

    units = DistanceUnit.KILOMETERS.value
    if isinstance(request.user, User):
        try:
            units = request.user.profile.effective_distance_units
        except (AttributeError, DatabaseError):
            units = DistanceUnit.KILOMETERS.value
    return {"distance_units": units}


def add_keyboard_shortcuts(request: HttpRequest) -> dict[str, dict[str, str]]:
    """Expose the viewer's shortcut overrides to templates.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``keyboard_shortcuts`` (profile overrides, or ``{}``).
    """
    if isinstance(request.user, User):
        try:
            return {"keyboard_shortcuts": request.user.profile.keyboard_shortcuts or {}}
        except (AttributeError, DatabaseError):
            pass
    return {"keyboard_shortcuts": {}}


def add_pending_account_deletion(request: HttpRequest) -> dict[str, object]:
    """Expose pending-deletion state for the warning banner.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with pending flag, date, and days left.
    """
    if isinstance(request.user, User):
        try:
            profile = request.user.profile
            if profile.is_pending_deletion:
                return {
                    "pending_account_deletion": True,
                    "account_deletion_date": profile.deletion_scheduled_for,
                    "account_deletion_days_left": profile.deletion_days_remaining,
                }
        except (AttributeError, DatabaseError):
            pass
    return {"pending_account_deletion": False, "account_deletion_date": None, "account_deletion_days_left": None}


def add_direct_messages(request: HttpRequest) -> dict[str, bool]:
    """Expose whether the navbar messages icon should render.

    Shown once the feature has been relevant; stays visible afterwards.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``show_messages_icon`` and ``e2ee_needs_oauth_enroll``.
    """
    if isinstance(request.user, User):
        try:
            from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
            from urbanlens.dashboard.models.friendship import Friendship
            from urbanlens.dashboard.services.messaging.direct_messages import has_used_direct_messages

            needs_oauth_enroll = not request.user.has_usable_password() and not MessagingKeyBundle.objects.filter(profile__user=request.user).exists()
            show_messages_icon = has_used_direct_messages(request.user.profile) or Friendship.objects.profile(request.user.profile).ever_friends().exists()
            return {
                "show_messages_icon": show_messages_icon,
                "e2ee_needs_oauth_enroll": needs_oauth_enroll,
            }
        except (ImportError, AttributeError, DatabaseError):
            pass
    return {"show_messages_icon": False, "e2ee_needs_oauth_enroll": False}


def add_feature_access(request: HttpRequest) -> dict[str, bool]:
    """Expose subscription-gated feature visibility to templates."""
    try:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

        return {
            "can_use_ai_features": user_has_feature(request.user, SiteFeature.AI),
            "show_places_layer": user_has_feature(request.user, SiteFeature.PLACES),
            "can_use_web_search": user_has_feature(request.user, SiteFeature.SEARCH),
            "can_upload_videos": user_has_feature(request.user, SiteFeature.VIDEO_UPLOADS),
            "can_upload_documents": user_has_feature(request.user, SiteFeature.DOCUMENT_UPLOADS),
            "show_games_nav": user_has_feature(request.user, SiteFeature.ALPHA_FEATURES),
            "has_beta_features": user_has_feature(request.user, SiteFeature.BETA_FEATURES),
        }
    except (ImportError, DatabaseError):
        return {"can_use_ai_features": False, "show_places_layer": False, "can_use_web_search": False, "can_upload_videos": False, "show_games_nav": False, "has_beta_features": False}
