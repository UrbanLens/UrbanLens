"""Site administration panel controller."""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.trips.model import SEARCH_PROVIDER_CHOICES, SiteSettings

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

        valid_providers = {v for v, _ in SEARCH_PROVIDER_CHOICES}
        provider = request.POST.get("search_provider", "")
        if provider in valid_providers:
            settings.search_provider = provider

        settings.save()

        return HttpResponseRedirect(reverse("dashboard:site_admin") + "?saved=1")
