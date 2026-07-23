# Generic imports
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views import View
from djangofoundry.controllers import ListController

from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.home_widgets import effective_widget_layout, home_dashboard_context, save_widget_layout

if TYPE_CHECKING:
    from django.http import HttpRequest


class IndexController(ListController):
    template_name = "dashboard/pages/home/index.html"
    model = Profile

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("home.view")
        return super().get(request, *args, **kwargs)

    @staticmethod
    def page_not_found(request, _exception=None):
        """Project-wide 404 handler - renders the standard error page."""
        return render(request, "dashboard/pages/errors/404.html", status=404)


class HomeOverviewView(LoginRequiredMixin, View):
    """The logged-in homepage: a customizable dashboard overview.

    GET /dashboard/home/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the dashboard overview for the signed-in user.

        Args:
            request: The authenticated request.

        Returns:
            The rendered homepage.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        display_name = profile.first_name or profile.username
        layout = effective_widget_layout(profile)
        context: dict[str, object] = {
            "profile": profile,
            "page_name": "home-overview",
            "hero_title": f"Welcome back, {display_name}",
            "home_widget_layout": layout,
            "home_widget_priority_items": [(entry["widget"].key, entry["widget"].label, entry["enabled"]) for entry in layout],
            "home_widget_current_value": ",".join(entry["widget"].key for entry in layout if entry["enabled"]),
            **home_dashboard_context(profile),
        }
        return render(request, "dashboard/pages/home/overview.html", context)


class HomeWidgetLayoutSaveView(LoginRequiredMixin, View):
    """Persist the signed-in user's chosen homepage widget selection/order.

    POST /dashboard/home/widgets/  body: ``{"enabled_keys": ["stats", ...]}``
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        enabled_keys = [str(key) for key in body.get("enabled_keys", []) if isinstance(key, str)]
        saved_keys = save_widget_layout(profile, enabled_keys)
        return JsonResponse({"enabled_keys": saved_keys})
