"""Organize controller - unified Tags + Categories management page."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User as AuthUser
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, KIND_USER, Badge

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_PERM = "dashboard.edit_global_badge"
_VALID_ORGANIZE_TABS = frozenset({"tags", "categories", "status", "people", "priority"})

_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def build_organize_page_context(request: HttpRequest, active_tab: str = "tags") -> dict:
    """Build template context shared by the Organize page and per-kind standalone pages.

    Args:
        request: The HTTP request (used for profile and permissions).
        active_tab: Tab to show as active (tags, categories, status, people, or priority).

    Returns:
        Context dict for dashboard/pages/organize/index.html.
    """
    if not isinstance(request.user, AuthUser):
        raise TypeError("Expected an authenticated user")
    profile: Profile = request.user.profile
    tags = Badge.objects.tags().visible_to(profile).ordered().with_customizations_for(profile).with_pin_counts()
    categories = Badge.objects.categories().for_profile(profile).ordered().with_customizations_for(profile).with_pin_counts()
    statuses = Badge.objects.statuses().for_profile(profile).ordered().with_customizations_for(profile).with_pin_counts()
    user_badges = Badge.objects.user_badges().visible_to(profile).ordered().with_customizations_for(profile)
    priority_items = Badge.objects.visible_to(profile).exclude(kind=KIND_USER).ordered().with_pin_counts()

    return {
        **_BASE_CTX,
        "tags": tags,
        "categories": categories,
        "statuses": statuses,
        "user_badges": user_badges,
        "priority_items": priority_items,
        "active_tab": active_tab,
        "can_edit_global": request.user.has_perm(_PERM),
        "standalone_mode": False,
    }


class OrganizeIndexView(LoginRequiredMixin, View):
    """Unified Organize page with Tags, Categories, and Priority tabs."""

    def get(self, request, *args, **kwargs):
        """Render the organize page.

        Args:
            request: The HTTP request. Accepts ?tab=tags|categories|status|people|priority.

        Returns:
            Rendered organize/index.html.
        """
        tab = request.GET.get("tab", "tags")
        if tab not in _VALID_ORGANIZE_TABS:
            tab = "tags"
        return render(request, "dashboard/pages/organize/index.html", build_organize_page_context(request, tab))


class OrganizePriorityListView(LoginRequiredMixin, View):
    """Re-render the Display Order tab's priority list.

    GET /organize/priority/list/

    The initial page load renders this list once; any badge create/edit/delete/
    merge/bulk-edit/convert elsewhere on the Organize page fires a `refreshPriority`
    client-side event (see organize.ts) that re-fetches it here, so a renamed,
    re-icon'd, deleted, or newly-created badge shows up without a full reload.
    """

    def get(self, request, *args, **kwargs):
        """Render the priority-list partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered `_priority_list.html` partial.
        """
        if not isinstance(request.user, AuthUser):
            raise TypeError("Expected an authenticated user")
        profile: Profile = request.user.profile
        priority_items = Badge.objects.visible_to(profile).exclude(kind=KIND_USER).ordered().with_pin_counts()
        return render(request, "dashboard/partials/badges/_priority_list.html", {"priority_items": priority_items})


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
