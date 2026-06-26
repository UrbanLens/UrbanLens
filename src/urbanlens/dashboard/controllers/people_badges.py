"""People badge controller - CRUD for KIND_USER badges (private profile labels)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Case, IntegerField, Value, When
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, KIND_USER, Badge

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.queryset import BadgeQuerySet
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_CTX_BASE = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def _people_badges(profile: Profile) -> BadgeQuerySet:
    """Return the user's visible KIND_USER badges in display order."""
    return Badge.objects.user_badges().visible_to(profile).ordered()


def _selected_parents_first(queryset, parent_ids):
    return queryset.annotate(
        _selected_parent=Case(
            When(id__in=parent_ids, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
    ).order_by("_selected_parent", "-order", "name", "id")


def _rows_ctx(profile: Profile) -> dict:
    """Build template context for the people badge rows partial."""
    return {**_CTX_BASE, "user_badges": _people_badges(profile)}


class PeopleBadgeRowsView(LoginRequiredMixin, View):
    """Return the people-badge-rows partial (re-render after CRUD operations)."""

    def get(self, request, *args, **kwargs):
        """Render and return the people badge rows partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered people_badge_rows.html partial.
        """
        profile = request.user.profile
        return render(request, "dashboard/partials/people_badge_rows.html", _rows_ctx(profile))


class PeopleBadgeCreateView(LoginRequiredMixin, View):
    """Load the create form (GET) or create a new KIND_USER badge (POST)."""

    def get(self, request, *args, **kwargs):
        """Return the create form for a new people badge.

        Args:
            request: The HTTP request.

        Returns:
            Rendered people_badge_form.html with no badge instance.
        """
        available_parents = Badge.objects.visible_to(request.user.profile).order_by("-order", "name", "id")
        return render(
            request,
            "dashboard/partials/people_badge_form.html",
            {**_CTX_BASE, "available_parents": available_parents, "parent_ids": set()},
        )

    def post(self, request, *args, **kwargs):
        """Create a new KIND_USER badge and return refreshed rows.

        Args:
            request: The HTTP request with name, icon, color POST data.

        Returns:
            Rendered people_badge_rows.html partial.
        """
        profile = request.user.profile
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        badge = Badge.objects.create(
            kind=KIND_USER,
            profile=profile,
            name=name,
            icon=request.POST.get("icon") or None,
            color=request.POST.get("color") or None,
        )
        parent_ids = request.POST.getlist("parent_ids")
        if parent_ids:
            valid_parents = Badge.objects.visible_to(profile).filter(id__in=parent_ids).exclude(id=badge.id)
            badge.parents.set(valid_parents)

        return render(request, "dashboard/partials/people_badge_rows.html", _rows_ctx(profile))


class PeopleBadgeEditView(LoginRequiredMixin, View):
    """Load the edit form (GET) or save changes (POST) for a KIND_USER badge."""

    def _get_badge(self, request, badge_id):
        badge = get_object_or_404(Badge, id=badge_id, kind=KIND_USER)
        if badge.profile is None:
            return None, HttpResponseForbidden("Global people labels cannot be edited here.")
        if badge.profile.user != request.user:
            return None, HttpResponseForbidden()
        return badge, None

    def get(self, request, badge_id, *args, **kwargs):
        """Return the edit form for an existing people badge.

        Args:
            request: The HTTP request.
            badge_id: PK of the Badge to edit.

        Returns:
            Rendered people_badge_form.html with the badge instance.
        """
        badge, err = self._get_badge(request, badge_id)
        if err:
            return err
        parent_ids = set(badge.parents.values_list("id", flat=True))
        available_parents = _selected_parents_first(
            Badge.objects.visible_to(request.user.profile).exclude(id=badge.id),
            parent_ids,
        )
        return render(
            request,
            "dashboard/partials/people_badge_form.html",
            {**_CTX_BASE, "badge": badge, "available_parents": available_parents, "parent_ids": parent_ids},
        )

    def post(self, request, badge_id, *args, **kwargs):
        """Save changes to a people badge and return refreshed rows.

        Args:
            request: The HTTP request with name, icon, color POST data.
            badge_id: PK of the Badge to edit.

        Returns:
            Rendered people_badge_rows.html partial.
        """
        badge, err = self._get_badge(request, badge_id)
        if err:
            return err

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        badge.name = name
        badge.icon = request.POST.get("icon") or None
        badge.color = request.POST.get("color") or None
        badge.save(update_fields=["name", "icon", "color", "updated"])
        parent_ids = request.POST.getlist("parent_ids")
        valid_parents = Badge.objects.visible_to(request.user.profile).filter(id__in=parent_ids).exclude(id=badge.id)
        badge.parents.set(valid_parents)

        return render(request, "dashboard/partials/people_badge_rows.html", _rows_ctx(request.user.profile))


class PeopleBadgeDeleteView(LoginRequiredMixin, View):
    """Delete a KIND_USER badge (HTMX - swaps the row out of the DOM)."""

    def post(self, request, badge_id, *args, **kwargs):
        """Delete the badge and return an empty response to remove its row.

        Args:
            request: The HTTP request.
            badge_id: PK of the Badge to delete.

        Returns:
            Empty 200 response (HTMX outerHTML swap removes the row).
        """
        badge = get_object_or_404(Badge, id=badge_id, kind=KIND_USER)
        if badge.profile is None or badge.profile.user != request.user:
            return HttpResponseForbidden()
        badge.delete()
        return render(request, "dashboard/partials/people_badge_rows.html", _rows_ctx(request.user.profile))


class PeopleBadgeMultiMergeView(LoginRequiredMixin, View):
    """Merge multiple KIND_USER badges into a surviving badge.

    Expects JSON body: {"target_id": int, "source_ids": [int, ...]}
    All profile assignments on source badges are transferred to the target before deletion.
    """

    def post(self, request, *args, **kwargs):
        """Perform the merge and return refreshed rows.

        Args:
            request: The HTTP request with JSON body.

        Returns:
            Rendered people_badge_rows.html partial, or error response.
        """
        profile = request.user.profile
        try:
            data = json.loads(request.body)
            target_id = int(data["target_id"])
            source_ids = [int(x) for x in data.get("source_ids", [])]
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return HttpResponse("Invalid data.", status=400)

        target = get_object_or_404(Badge, id=target_id, kind=KIND_USER, profile=profile)
        sources = list(
            Badge.objects.filter(id__in=source_ids, kind=KIND_USER, profile=profile).exclude(id=target_id),
        )

        from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment

        for source in sources:
            for assignment in ProfileBadgeAssignment.objects.filter(badge=source):
                ProfileBadgeAssignment.objects.get_or_create(
                    author=assignment.author,
                    subject=assignment.subject,
                    badge=target,
                )
            source.delete()

        return render(request, "dashboard/partials/people_badge_rows.html", _rows_ctx(profile))
