"""First-run setup wizard controller."""

from __future__ import annotations

import hashlib
import logging

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.site_admin import complete_site_admin_onboarding

logger = logging.getLogger(__name__)


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

        from urbanlens.dashboard.services.social_auth.pipeline import random_emoji_options
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        profile = request.user.profile
        email = request.user.email or ""
        gravatar_preview_url = ""
        if email:
            gh = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
            gravatar_preview_url = f"https://www.gravatar.com/avatar/{gh}?s=200&d=identicon"

        return render(
            request,
            "dashboard/pages/setup/index.html",
            {
                "settings": site,
                "features": _build_feature_groups(app_settings),
                "page_name": "setup",
                "emoji_options": random_emoji_options(4),
                "gravatar_preview_url": gravatar_preview_url,
                "current_username": request.user.username,
                "current_avatar_url": profile.avatar.url if profile.avatar else "",
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
            if title:
                site.app_title = title
                site.save(update_fields=["app_title"])
            return HttpResponse(status=204)

        if action == "complete":
            complete_site_admin_onboarding(request.user)
            return HttpResponseRedirect(reverse("map.view"))

        return HttpResponse(status=400)
