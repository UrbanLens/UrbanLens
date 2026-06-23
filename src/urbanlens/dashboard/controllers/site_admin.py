"""Site administration panel controller."""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.site_settings import SEARCH_PROVIDER_CHOICES, SiteSettings
from urbanlens.dashboard.services.site_admin import complete_site_admin_onboarding

logger = logging.getLogger(__name__)


class SiteAdminView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Site admin settings page.

    Requires the ``dashboard.view_site_admin`` permission (superusers bypass
    this automatically via Django's permission system).

    GET  /site-admin/  → settings page
    POST /site-admin/  → save settings, re-render page
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def get(self, request):
        settings = SiteSettings.get_current()
        complete_site_admin_onboarding(request.user)
        return render(
            request,
            "dashboard/pages/site_admin.html",
            {
                "settings": settings,
                "page_name": "site-admin",
                "saved": request.GET.get("saved"),
                "search_provider_choices": SEARCH_PROVIDER_CHOICES,
            },
        )

    def post(self, request):
        settings = SiteSettings.get_current()

        try:
            max_members = int(request.POST.get("max_trip_members", settings.max_trip_members))
            settings.max_trip_members = max(max_members, 1)
        except (ValueError, TypeError):
            pass

        try:
            max_bbox = float(request.POST.get("max_bbox_area_km2", settings.max_bbox_area_km2))
            if max_bbox > 0:
                settings.max_bbox_area_km2 = max_bbox
        except (ValueError, TypeError):
            pass

        app_title = request.POST.get("app_title", "").strip()
        if app_title:
            settings.app_title = app_title

        valid_providers = {v for v, _ in SEARCH_PROVIDER_CHOICES}
        provider = request.POST.get("search_provider", "")
        if provider in valid_providers:
            settings.search_provider = provider

        try:
            cache_hours = int(request.POST.get("search_cache_hours", settings.search_cache_hours))
            settings.search_cache_hours = max(0, cache_hours)
        except (ValueError, TypeError):
            pass

        try:
            max_attempts = int(request.POST.get("login_max_attempts", settings.login_max_attempts))
            settings.login_max_attempts = max(0, max_attempts)
        except (ValueError, TypeError):
            pass

        try:
            lockout_minutes = int(request.POST.get("login_lockout_minutes", settings.login_lockout_minutes))
            settings.login_lockout_minutes = max(1, lockout_minutes)
        except (ValueError, TypeError):
            pass

        settings.save()

        return HttpResponseRedirect(reverse("site_admin") + "?saved=1")
