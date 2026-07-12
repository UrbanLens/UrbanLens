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


#: URL-name prefixes that belong to a nav-bar section other than their own, e.g.
#: pin detail pages (``pin.*``) are reached from the map and should keep "Map" active.
_NAV_SECTION_ALIASES = {"pin": "map"}


def add_page_name(request: HttpRequest) -> dict[str, str]:
    """Expose the current page and nav-bar section to every template.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``page_name`` (the resolved URL name, sanitized for use as a
        CSS class) and ``nav_section`` (the URL name's leading ``section.``
        segment, used by the nav bar to highlight the active link).
    """
    resolver_match = request.resolver_match
    if resolver_match is None:
        return {"page_name": "", "nav_section": ""}
    url_name = resolver_match.url_name or ""
    # This will be a className, so replace anything that would trip up css
    page_name = re.sub(r"[^a-zA-Z0-9]", "-", url_name)
    section = url_name.split(".", 1)[0] if url_name else ""
    nav_section = _NAV_SECTION_ALIASES.get(section, section)
    return {"page_name": page_name, "nav_section": nav_section}


def add_distance_units(request: HttpRequest) -> dict[str, str]:
    """Expose the viewer's effective distance unit to every template.

    Templates render distances (stored internally in kilometres) in this unit via
    the ``distance`` filter, e.g. ``{{ value_km|distance:distance_units }}``.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``distance_units`` ("km" or "mi"), defaulting to "km" for
        anonymous users or when the profile is unavailable.
    """
    from urbanlens.dashboard.models.profile.meta import DistanceUnit

    units = DistanceUnit.KILOMETERS.value
    if isinstance(request.user, User):
        try:
            units = request.user.profile.effective_distance_units
        except (AttributeError, DatabaseError):
            units = DistanceUnit.KILOMETERS.value
    return {"distance_units": units}


def add_pending_account_deletion(request: HttpRequest) -> dict[str, object]:
    """Expose the current user's pending-deletion state for the site-wide warning banner.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``pending_account_deletion`` (bool), ``account_deletion_date``
        (datetime or None), and ``account_deletion_days_left`` (int or None).
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
    """Expose whether the navbar messages icon should render for this user.

    The icon only appears once the user has ever sent or received a direct
    message - users who have never touched the feature don't get an extra
    navbar icon competing for attention.

    Args:
        request: The current HttpRequest.

    Returns:
        dict with ``show_messages_icon`` (bool) and ``e2ee_needs_oauth_enroll``
        (bool - True for passwordless accounts with no key bundle yet, which
        base.html enrolls transparently in the background).
    """
    if isinstance(request.user, User):
        try:
            from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
            from urbanlens.dashboard.services.direct_messages import has_used_direct_messages

            needs_oauth_enroll = not request.user.has_usable_password() and not MessagingKeyBundle.objects.filter(profile__user=request.user).exists()
            return {
                "show_messages_icon": has_used_direct_messages(request.user.profile),
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
        }
    except (ImportError, DatabaseError):
        return {"can_use_ai_features": False, "show_places_layer": False, "can_use_web_search": False}
