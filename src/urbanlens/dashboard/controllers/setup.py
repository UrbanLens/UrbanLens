"""First-run setup wizard controller."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import complete_site_admin_onboarding

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_RESERVED_APP_TITLE_NORMALIZED = "urbanlens"
_TITLE_NORMALIZE_RE = re.compile(r"[^a-zA-Z0-9]+")


def _request_host(request) -> str:
    """
    Return the request hostname without port or leading ``www.``.

    Reads directly from META to avoid DisallowedHost exceptions; this function is used only for branding
    decisions, not security validation.

    Args:
        request: The current HttpRequest.

    Returns:
        Lowercase hostname.
    """
    host = request.META.get("HTTP_HOST", "").split(":")[0].lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _official_site_host() -> str:
    """
    Return the flagship host from ``UL_OFFICIAL_SITE_HOST``, normalized for comparison.

    Returns:
        Lowercase hostname without a leading ``www.``, or an empty string when no instance is flagship.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    host = app_settings.official_site_host.strip().lower()
    if host.startswith("www."):
        return host[4:]
    return host


def is_official_urbanlens_site(request) -> bool:
    """
    Whether this request is served from the flagship instance.

    Args:
        request: The current HttpRequest.

    Returns:
        True when a flagship host is configured and this request's host is that host.
    """
    official = _official_site_host()
    return bool(official) and _request_host(request) == official


def app_title_is_reserved(request) -> bool:
    """
    Whether this instance is asked not to reuse the flagship's app name.

    Args:
        request: The current HttpRequest.

    Returns:
        True when a flagship host is configured and this request is not served from it.
    """
    return bool(_official_site_host()) and not is_official_urbanlens_site(request)


def normalize_app_title(title: str) -> str:
    """
    Strip spaces and punctuation, then lowercase, for reserved-name checks.

    Args:
        title: Raw app title from the user.

    Returns:
        Alphanumeric-only lowercase string.
    """
    return _TITLE_NORMALIZE_RE.sub("", title.lower())


def is_reserved_urbanlens_title(title: str) -> bool:
    """
    Whether ``title`` matches the reserved ``UrbanLens`` name.

    Args:
        title: Raw app title from the user.

    Returns:
        True when the normalized title is ``urbanlens``.
    """
    return normalize_app_title(title) == _RESERVED_APP_TITLE_NORMALIZED


def personalized_map_title(user: User) -> str:
    """
    Build a default app title from the user's first name or username.

    Args:
        user: The bootstrap administrator.

    Returns:
        A possessive title based on the display name.
    """
    first = (user.first_name or "").strip()
    if first:
        display_name = first.split()[0]
        if display_name:
            return f"{display_name}'s Map"
    return f"{user.username}'s Map"


def app_title_name_suggestions(user: User) -> list[str]:
    """
    Return alternative app-title ideas for non-official instances.

    Args:
        user: The bootstrap administrator.

    Returns:
        Human-readable title suggestions, personalized to the admin user.
    """
    first = (user.first_name or "").strip()
    display_name = first.split()[0] if first else user.username
    return [
        f"{display_name}'s Map",
        f"{display_name}'s Urbex Atlas",
        "Private Lens",
        "Urbex Tracker",
        "Urban Atlas",
    ]


def setup_app_title_value(request: HttpRequest, user: User, current_title: str) -> str:
    """
    Resolve the app title shown in the setup wizard.

    Where the flagship's app name is reserved, replace the factory default ``UrbanLens`` with a personalized
    suggestion so installers are not nudged toward the reserved name.

    Args:
        request: The current HttpRequest.
        user: The bootstrap administrator.
        current_title: ``SiteSettings.app_title`` from the database.

    Returns:
        Title string for the setup form.
    """
    if not app_title_is_reserved(request):
        return current_title
    if is_reserved_urbanlens_title(current_title):
        return personalized_map_title(user)
    return current_title


def urbanlens_title_notice(official_host: str) -> str:
    """
    Explain why the app name is reserved on this instance.

    Args:
        official_host: The flagship host named by ``UL_OFFICIAL_SITE_HOST``.

    Returns:
        Notice text for the setup wizard.
    """
    return f"UrbanLens is the name of the public project at {official_host}. For your private instance, please choose a different name so visitors are not confused about which site they are on."


def _build_feature_groups(app_settings) -> list[dict]:
    """
    Build the feature-availability matrix for the integrations step.

    Args:
        app_settings: The AppSettings singleton.

    Returns:
        A list of feature group dicts with label, icon, and items, plus the groups plugins add.
    """
    from urbanlens.dashboard.plugins.points import SETUP_FEATURE_GROUPS

    groups = [
        {
            "label": "Maps & Geocoding",
            "icon": "map",
            "items": [
                {
                    "name": "Google Maps",
                    "description": "Google Maps tile layer and map controls",
                    "env_var": "UL_GOOGLE_UNRESTRICTED_API_KEY",
                    "configured": bool(app_settings.google_unrestricted_api_key),
                },
                {
                    "name": "Google Places",
                    "description": "Address autocomplete and place details lookup",
                    "env_var": "UL_GOOGLE_UNRESTRICTED_API_KEY",
                    "configured": bool(app_settings.google_unrestricted_api_key),
                },
                {
                    "name": "Google Street View",
                    "description": "Street-level imagery on Private Pin pages",
                    "env_var": "UL_GOOGLE_UNRESTRICTED_API_KEY",
                    "configured": bool(app_settings.google_unrestricted_api_key),
                },
            ],
        },
        {
            "label": "Web Search",
            "icon": "travel_explore",
            "items": [
                {
                    "name": "Google Search",
                    "description": "Google Custom Search for pin-related web results",
                    "env_var": "UL_GOOGLE_UNRESTRICTED_API_KEY + UL_GOOGLE_SEARCH_TENANT",
                    "configured": bool(app_settings.google_unrestricted_api_key and app_settings.google_search_tenant),
                },
            ],
        },
        {
            "label": "Weather",
            "icon": "wb_sunny",
            "items": [
                {
                    "name": "OpenWeatherMap",
                    "description": "Current and historical weather data for pin locations",
                    "env_var": "UL_OPENWEATHERMAP_API_KEY",
                    "configured": bool(app_settings.openweathermap_api_key),
                },
            ],
        },
        {
            "label": "Social Login",
            "icon": "login",
            "items": [
                {
                    "name": "Google OAuth",
                    "description": "Allow users to sign in with their Google account",
                    "env_var": "UL_GOOGLE_CLIENT_ID + UL_GOOGLE_CLIENT_SECRET",
                    "configured": bool(app_settings.google_client_id and app_settings.google_client_secret),
                },
                {
                    "name": "Discord OAuth",
                    "description": "Allow users to sign in with their Discord account",
                    "env_var": "UL_DISCORD_CLIENT_ID + UL_DISCORD_CLIENT_SECRET",
                    "configured": bool(app_settings.discord_client_id and app_settings.discord_client_secret),
                },
            ],
        },
        {
            "label": "Email (SMTP)",
            "icon": "email",
            "items": [
                {
                    "name": "SMTP Server",
                    "description": "Outbound email for account verification, password resets, and friend invitations",
                    "env_var": "UL_EMAIL_HOST + UL_EMAIL_USER + UL_EMAIL_PASSWORD",
                    "configured": bool(app_settings.email_host and app_settings.email_user and app_settings.email_password),
                },
                {
                    "name": "Sender Address",
                    "description": "The From address shown on outgoing emails",
                    "env_var": "UL_EMAIL_FROM",
                    "configured": bool(app_settings.email_from and app_settings.email_from != type(app_settings).model_fields["email_from"].default),
                },
            ],
        },
    ]
    return SETUP_FEATURE_GROUPS.apply(groups)


class SetupWizardView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """
    First-run setup wizard for the bootstrap administrator.

    GET /setup/ → render wizard
    POST /setup/ → handle action (save_title | complete)

    Only accessible while ``SiteSettings.bootstrap_admin_onboarding_complete`` is False.
    Once complete, all visits redirect to the map.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def get(self, request):
        """
        Render the setup wizard.

        Args:
            request: The current HttpRequest.

        Returns:
            HttpResponse with the wizard template, or a redirect if already complete.
        """
        site = SiteSettings.get_current()
        if site.bootstrap_admin_onboarding_complete:
            return redirect("map.view")

        from urbanlens.dashboard.services.profile.avatar import AvatarService, gravatar_preview_url
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        profile = request.user.profile

        title_reserved = app_title_is_reserved(request)
        suggested_title = personalized_map_title(request.user)
        setup_title = setup_app_title_value(request, request.user, site.app_title)
        if setup_title != site.app_title:
            site.app_title = setup_title
            site.save(update_fields=["app_title"])
        title_suggestions = app_title_name_suggestions(request.user)

        return render(
            request,
            "dashboard/pages/setup/index.html",
            {
                "settings": site,
                "features": _build_feature_groups(app_settings),
                "page_name": "setup",
                "emoji_options": AvatarService.random_options(4),
                "gravatar_preview_url": gravatar_preview_url(request.user.email or ""),
                "current_username": request.user.username,
                "current_avatar_url": profile.avatar.url if profile.avatar else "",
                "title_reserved": title_reserved,
                "suggested_app_title": suggested_title,
                "setup_app_title": setup_title,
                "app_title_suggestions": title_suggestions,
                "urbanlens_title_notice": urbanlens_title_notice(_official_site_host()),
            },
        )

    def post(self, request):
        """
        Handle wizard POST actions.

        Args:
            request: The current HttpRequest.

        Returns:
            HttpResponse (200 for save_title) or redirect (for complete).
        """
        action = request.POST.get("action", "")
        site = SiteSettings.get_current()

        if action == "save_title":
            title = request.POST.get("app_title", "").strip()
            if not title:
                return HttpResponse(status=400)
            if app_title_is_reserved(request) and is_reserved_urbanlens_title(title):
                logger.warning("Reserved UrbanLens title (%s) on domain (%s)", title, _request_host(request))
                return JsonResponse(
                    {
                        "error": urbanlens_title_notice(_official_site_host()),
                        "suggestions": app_title_name_suggestions(request.user),
                    },
                    status=400,
                )
            site.app_title = title
            site.save(update_fields=["app_title"])
            return HttpResponse(status=204)

        if action == "complete":
            if app_title_is_reserved(request) and is_reserved_urbanlens_title(site.app_title):
                logger.warning("Reserved UrbanLens title (%s) on domain (%s)", site.app_title, _request_host(request))
                return JsonResponse(
                    {
                        "error": urbanlens_title_notice(_official_site_host()),
                        "suggestions": app_title_name_suggestions(request.user),
                    },
                    status=400,
                )
            complete_site_admin_onboarding(request.user)
            return HttpResponseRedirect(reverse("site_admin"))

        return HttpResponse(status=400)
