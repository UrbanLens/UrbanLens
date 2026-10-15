"""First-run setup wizard controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import complete_site_admin_onboarding

if TYPE_CHECKING:
    from django.contrib.auth.models import User


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

        suggested_title = personalized_map_title(request.user)

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
                "suggested_app_title": suggested_title,
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
            site.app_title = title
            site.save(update_fields=["app_title"])
            return HttpResponse(status=204)

        if action == "complete":
            complete_site_admin_onboarding(request.user)
            return HttpResponseRedirect(reverse("site_admin"))

        return HttpResponse(status=400)
