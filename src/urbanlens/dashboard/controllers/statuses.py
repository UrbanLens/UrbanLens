"""Status controller - CRUD for personal status badges."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, Badge
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def _rows_ctx(profile, extra: dict | None = None) -> dict:
    statuses = (
        Badge.objects.statuses()
        .for_profile(profile)
        .ordered()
        .with_pin_counts()
    )
    ctx = {**_BASE_CTX, "statuses": statuses}
    if extra:
        ctx.update(extra)
    return ctx


class StatusRowsView(LoginRequiredMixin, View):
    """Return the status-rows partial."""

    def get(self, request, *args, **kwargs):
        """Render and return the status rows partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered status_rows.html partial.
        """
        profile = request.user.profile
        return render(request, "dashboard/partials/status_rows.html", _rows_ctx(profile))


class StatusCreateView(LoginRequiredMixin, View):
    """Create a new personal status badge (HTMX)."""

    def post(self, request, *args, **kwargs):
        """Create a status badge and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.

        Returns:
            Rendered status_rows.html partial, or 400 on validation failure.
        """
        profile = request.user.profile
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        badge = Badge.objects.create(
            kind="status",
            profile=profile,
            name=name,
            description=request.POST.get("description", "").strip() or None,
            icon=request.POST.get("icon") or None,
            color=request.POST.get("color") or None,
            order=int(request.POST.get("order", 0)),
        )
        return render(
            request,
            "dashboard/partials/status_rows.html",
            _rows_ctx(profile, {"new_status_id": badge.id}),
        )


class StatusEditView(LoginRequiredMixin, View):
    """Edit an existing personal status badge (HTMX)."""

    def get(self, request, status_id, *args, **kwargs):
        """Return the edit form partial for a status badge.

        Args:
            request: The HTTP request.
            status_id: Badge PK.

        Returns:
            Rendered status_edit_form.html partial.
        """
        badge = get_object_or_404(Badge, id=status_id, kind="status")
        if badge.profile is None or badge.profile.user != request.user:
            return HttpResponseForbidden()
        return render(
            request,
            "dashboard/partials/status_edit_form.html",
            {**_BASE_CTX, "badge": badge},
        )

    def post(self, request, status_id, *args, **kwargs):
        """Save edits and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.
            status_id: Badge PK.

        Returns:
            Rendered status_rows.html partial.
        """
        badge = get_object_or_404(Badge, id=status_id, kind="status")
        if badge.profile is None or badge.profile.user != request.user:
            return HttpResponseForbidden()

        new_kind = request.POST.get("kind", "status")
        if new_kind not in {"tag", "category", "status"}:
            new_kind = "status"
        kind_changed = new_kind != badge.kind

        if kind_changed and badge.is_protected:
            return HttpResponse("Protected statuses cannot be converted to another type.", status=403)

        # Protected badges may not be renamed.
        if not badge.is_protected:
            name = request.POST.get("name", "").strip()
            if not name:
                return HttpResponse("Name is required.", status=400)
            badge.name = name

        badge.description = request.POST.get("description", "").strip() or None
        badge.icon = request.POST.get("icon") or None
        badge.color = request.POST.get("color") or None
        badge.order = int(request.POST.get("order", badge.order))

        profile = request.user.profile

        if kind_changed and new_kind == "tag":
            # Migrate status → tag: remove from pin.statuses, add to pin.tags.
            for pin in Pin.objects.filter(statuses=badge, profile=profile):
                pin.tags.add(badge)
                pin.statuses.remove(badge)
            badge.kind = "tag"
            badge.profile = profile

        elif kind_changed and new_kind == "category":
            # Migrate status → category: remove from pin.statuses, add to pin.categories. Make global.
            for pin in Pin.objects.filter(statuses=badge, profile=profile):
                pin.categories.add(badge)
                pin.statuses.remove(badge)
            badge.kind = "category"
            badge.profile = None

        badge.save()

        if kind_changed and new_kind == "tag":
            from django.urls import reverse
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("organize.index") + "?tab=tags"
            return response

        if kind_changed and new_kind == "category":
            from django.urls import reverse
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("organize.index") + "?tab=categories"
            return response

        return render(request, "dashboard/partials/status_rows.html", _rows_ctx(request.user.profile))


class StatusDeleteView(LoginRequiredMixin, View):
    """Delete a personal status badge (HTMX)."""

    def post(self, request, status_id, *args, **kwargs):
        """Delete the status badge and return the refreshed rows partial.

        Args:
            request: The HTTP request.
            status_id: Badge PK.

        Returns:
            Rendered status_rows.html partial, or 403 if protected.
        """
        badge = get_object_or_404(Badge, id=status_id, kind="status")
        if badge.profile is None or badge.profile.user != request.user:
            return HttpResponseForbidden()
        if badge.is_protected:
            return HttpResponse("The 'Visited' status cannot be deleted.", status=403)
        badge.delete()
        return render(request, "dashboard/partials/status_rows.html", _rows_ctx(request.user.profile))


class StatusMembershipView(LoginRequiredMixin, View):
    """Add or remove a status badge from a specific pin (HTMX panel on pin detail page)."""

    def get(self, request, pin_uuid, *args, **kwargs):
        """Render the status panel for a pin.

        Args:
            request: The HTTP request.
            pin_uuid: UUID of the target pin.

        Returns:
            Rendered status_panel.html partial.
        """
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        profile = request.user.profile
        all_statuses = Badge.objects.statuses().for_profile(profile).ordered()
        member_ids = set(pin.statuses.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/status_panel.html",
            {"pin": pin, "all_statuses": all_statuses, "member_ids": member_ids},
        )

    def post(self, request, pin_uuid, *args, **kwargs):
        """Toggle a status badge on a pin.

        Args:
            request: The HTTP request with POST data (status_id, action).
            pin_uuid: UUID of the target pin.

        Returns:
            Rendered status_panel.html partial.
        """
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        status_id = request.POST.get("status_id")
        action = request.POST.get("action")  # "add" or "remove"

        profile = request.user.profile
        badge = get_object_or_404(Badge.objects.statuses().for_profile(profile), id=status_id)

        if action == "add":
            pin.statuses.add(badge)
        elif action == "remove":
            pin.statuses.remove(badge)

        all_statuses = Badge.objects.statuses().for_profile(profile).ordered()
        member_ids = set(pin.statuses.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/status_panel.html",
            {"pin": pin, "all_statuses": all_statuses, "member_ids": member_ids},
        )


class StatusBulkDeleteView(LoginRequiredMixin, View):
    """Bulk-delete personal status badges (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Delete the specified status badges and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            Rendered status_rows.html partial.
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No statuses specified.", status=400)

        profile = request.user.profile
        # Never delete protected badges even if included in the bulk request.
        Badge.objects.filter(id__in=ids, profile=profile, kind="status", is_protected=False).delete()
        return render(request, "dashboard/partials/status_rows.html", _rows_ctx(profile))
