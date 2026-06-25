"""First-run setup wizard controller."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.site_admin import complete_site_admin_onboarding

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_OFFICIAL_URBANLENS_HOST = "urbanlens.org"
_RESERVED_APP_TITLE_NORMALIZED = "urbanlens"
_TITLE_NORMALIZE_RE = re.compile(r"[^a-zA-Z0-9]+")


def _request_host(request) -> str:
    """Return the request hostname without port or leading ``www.``.

    Reads directly from META to avoid DisallowedHost exceptions; this function
    is used only for branding decisions, not security validation.

    Args:
        request: The current HttpRequest.

    Returns:
        Lowercase hostname.
    """
    host = request.META.get("HTTP_HOST", "").split(":")[0].lower()
    if host.startswith("www."):
        return host[4:]
    return host


def is_official_urbanlens_site(request) -> bool:
    """Whether this request is served from the canonical UrbanLens domain.

    Args:
        request: The current HttpRequest.

    Returns:
        True when the host is ``urbanlens.org``.
    """
    return _request_host(request) == _OFFICIAL_URBANLENS_HOST


def normalize_app_title(title: str) -> str:
    """Strip spaces and punctuation, then lowercase, for reserved-name checks.

    Args:
        title: Raw app title from the user.

    Returns:
        Alphanumeric-only lowercase string.
    """
    return _TITLE_NORMALIZE_RE.sub("", title.lower())


def is_reserved_urbanlens_title(title: str) -> bool:
    """Whether ``title`` matches the reserved ``UrbanLens`` name.

    Args:
        title: Raw app title from the user.

    Returns:
        True when the normalized title is ``urbanlens``.
    """
    return normalize_app_title(title) == _RESERVED_APP_TITLE_NORMALIZED


def personalized_map_title(user: User) -> str:
    """Build a default app title from the user's first name or username.

    Args:
        user: The bootstrap administrator.

    Returns:
        A title such as ``Jess's Map``.
    """
    first = (user.first_name or "").strip()
    if first:
        display_name = first.split()[0]
        if display_name:
            return f"{display_name}'s Map"
    return f"{user.username}'s Map"


def app_title_name_suggestions(user: User) -> list[str]:
    """Return alternative app-title ideas for non-official instances.

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
    """Resolve the app title shown in the setup wizard.

    On non-official hosts, replace the factory default ``UrbanLens`` with a
    personalized suggestion so installers are not nudged toward the reserved name.

    Args:
        request: The current HttpRequest.
        user: The bootstrap administrator.
        current_title: ``SiteSettings.app_title`` from the database.

    Returns:
        Title string for the setup form.
    """
    if is_official_urbanlens_site(request):
        return current_title
    if is_reserved_urbanlens_title(current_title):
        return personalized_map_title(user)
    return current_title


URBANLENS_TITLE_NOTICE = (
    "UrbanLens is the name of the public project at urbanlens.org. "
    "For your private instance, please choose a different name so visitors "
    "are not confused about which site they are on."
)


def _build_feature_groups(app_settings) -> list[dict]:
    """Build the feature-availability matrix for the integrations step.

    Args:
        app_settings: The AppSettings singleton.

    Returns:
        A list of feature group dicts with label, icon, and items.
    """
    return [
        {
            "label": "AI & Smart Features",
            "icon": "psychology",
            "items": [
                {
                    "name": "OpenAI",
                    "description": "GPT-powered category suggestions and AI-generated descriptions",
                    "env_var": "UL_OPENAI_API_KEY",
                    "configured": bool(app_settings.openai_api_key),
                },
                {
                    "name": "Cloudflare AI",
                    "description": "Cloudflare Workers AI for fast, low-cost inference",
                    "env_var": "UL_CLOUDFLARE_AI_API_KEY + UL_CLOUDFLARE_AI_ENDPOINT",
                    "configured": bool(app_settings.cloudflare_ai_api_key and app_settings.cloudflare_ai_endpoint),
                },
                {
                    "name": "HuggingFace AI",
                    "description": "Open-source model inference via HuggingFace",
                    "env_var": "UL_HUGGINGFACE_AI_API_KEY",
                    "configured": bool(app_settings.huggingface_ai_api_key),
                },
            ],
        },
        {
            "label": "Maps & Geocoding",
            "icon": "map",
            "items": [
                {
                    "name": "Google Maps",
                    "description": "Google Maps tile layer and map controls",
                    "env_var": "UL_GOOGLE_MAPS_API_KEY",
                    "configured": bool(app_settings.google_maps_api_key),
                },
                {
                    "name": "Google Places",
                    "description": "Address autocomplete and place details lookup",
                    "env_var": "UL_GOOGLE_PLACES_API_KEY",
                    "configured": bool(app_settings.google_places_api_key),
                },
                {
                    "name": "Google Street View",
                    "description": "Street-level imagery on pin detail pages",
                    "env_var": "UL_GOOGLE_STREET_VIEW_API_KEY",
                    "configured": bool(app_settings.google_street_view_api_key),
                },
            ],
        },
        {
            "label": "Web Search",
            "icon": "travel_explore",
            "items": [
                {
                    "name": "Brave Search",
                    "description": "Privacy-focused web search results on pin pages",
                    "env_var": "UL_BRAVE_SEARCH_API_KEY",
                    "configured": bool(app_settings.brave_search_api_key),
                },
                {
                    "name": "Google Search",
                    "description": "Google Custom Search for pin-related web results",
                    "env_var": "UL_GOOGLE_SEARCH_API_KEY + UL_GOOGLE_SEARCH_TENANT",
                    "configured": bool(app_settings.google_search_api_key and app_settings.google_search_tenant),
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
            "label": "History & Culture",
            "icon": "museum",
            "items": [
                {
                    "name": "Smithsonian Institution",
                    "description": "Historical photos and records from Smithsonian collections",
                    "env_var": "UL_SMITHSONIAN_API_KEY",
                    "configured": bool(app_settings.smithsonian_api_key),
                },
                {
                    "name": "National Park Service",
                    "description": "NPS park information for locations near national parks",
                    "env_var": "UL_NPS_API_KEY",
                    "configured": bool(app_settings.nps_api_key),
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
                    "configured": bool(app_settings.email_from and app_settings.email_from != "jess@urbanlens.org"),
                },
            ],
        },
    ]


class SetupWizardView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """First-run setup wizard for the bootstrap administrator.

    Only accessible while ``SiteSettings.bootstrap_admin_onboarding_complete`` is False.
    Once complete, all visits redirect to the map.

    GET  /setup/  → render wizard
    POST /setup/  → handle action (save_title | complete)
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def get(self, request):
        """Render the setup wizard.

        Args:
            request: The current HttpRequest.

        Returns:
            HttpResponse with the wizard template, or a redirect if already complete.
        """
        site = SiteSettings.get_current()
        if site.bootstrap_admin_onboarding_complete:
            return redirect("map.view")

        from urbanlens.dashboard.services.avatar import AvatarService
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        profile = request.user.profile
        email = request.user.email or ""
        gravatar_preview_url = ""
        if email:
            gh = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
            gravatar_preview_url = f"https://www.gravatar.com/avatar/{gh}?s=200&d=identicon"

        official_site = is_official_urbanlens_site(request)
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
                "gravatar_preview_url": gravatar_preview_url,
                "current_username": request.user.username,
                "current_avatar_url": profile.avatar.url if profile.avatar else "",
                "is_official_urbanlens_site": official_site,
                "suggested_app_title": suggested_title,
                "setup_app_title": setup_title,
                "app_title_suggestions": title_suggestions,
                "urbanlens_title_notice": URBANLENS_TITLE_NOTICE,
            },
        )

    def post(self, request):
        """Handle wizard POST actions.

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
            if not is_official_urbanlens_site(request) and is_reserved_urbanlens_title(title):
                logger.warning("Reserved UrbanLens title (%s) on domain (%s)", title, _request_host(request))
                return JsonResponse(
                    {
                        "error": URBANLENS_TITLE_NOTICE,
                        "suggestions": app_title_name_suggestions(request.user),
                    },
                    status=400,
                )
            site.app_title = title
            site.save(update_fields=["app_title"])
            return HttpResponse(status=204)

        if action == "complete":
            if not is_official_urbanlens_site(request) and is_reserved_urbanlens_title(site.app_title):
                logger.warning("Reserved UrbanLens title (%s) on domain (%s)", site.app_title, _request_host(request))
                return JsonResponse(
                    {
                        "error": URBANLENS_TITLE_NOTICE,
                        "suggestions": app_title_name_suggestions(request.user),
                    },
                    status=400,
                )
            complete_site_admin_onboarding(request.user)
            return HttpResponseRedirect(reverse("site_admin"))

        return HttpResponse(status=400)
