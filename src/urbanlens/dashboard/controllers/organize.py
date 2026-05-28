"""Organize controller - unified Tags + Categories management page."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, Badge

logger = logging.getLogger(__name__)

_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


class OrganizeIndexView(LoginRequiredMixin, View):
    """Unified Organize page with Tags, Categories, and Priority tabs."""

    def get(self, request, *args, **kwargs):
        """Render the organize page.

        Args:
            request: The HTTP request. Accepts ?tab=tags|categories|priority.

        Returns:
            Rendered organize/index.html.
        """
        profile = request.user.profile
        tab = request.GET.get("tab", "tags")

        tags = (
            Badge.objects.tags()
            .visible_to(profile)
            .ordered()
            .with_customizations_for(profile)
            .with_pin_counts()
        )
        categories = (
            Badge.objects.categories()
            .ordered()
            .with_customizations_for(profile)
            .with_pin_counts()
        )
        # Priority list: all tags visible to the user + all categories, sorted by order desc then name.
        priority_items = (
            Badge.objects.visible_to(profile)
            .ordered()
            .with_pin_counts()
        )

        can_edit_global = request.user.has_perm("dashboard.edit_global_badge")
        return render(
            request,
            "dashboard/pages/organize/index.html",
            {
                **_BASE_CTX,
                "tags": tags,
                "categories": categories,
                "priority_items": priority_items,
                "active_tab": tab,
                "can_edit_global": can_edit_global,
            },
        )


class OrganizePrioritySaveView(LoginRequiredMixin, View):
    """Save the combined priority order for tags and categories."""

    def post(self, request, *args, **kwargs):
        """Persist new order for the submitted item IDs.

        Expects JSON body: {"items": [{"id": 1}, {"id": 2}, ...]} in display order
        (first item gets the highest order value).

        Args:
            request: The HTTP request with JSON body.

        Returns:
            JSON response with ok=True on success.
        """
        try:
            data = json.loads(request.body)
            item_ids = [int(x["id"]) for x in data.get("items", [])]
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not item_ids:
            return JsonResponse({"error": "No items provided"}, status=400)

        profile = request.user.profile
        visible_ids = set(
            Badge.objects.visible_to(profile).filter(id__in=item_ids).values_list("id", flat=True),
        )
        total = len(item_ids)
        for i, item_id in enumerate(item_ids):
            if item_id not in visible_ids:
                continue
            Badge.objects.filter(id=item_id).update(order=total - i)

        return JsonResponse({"ok": True})
