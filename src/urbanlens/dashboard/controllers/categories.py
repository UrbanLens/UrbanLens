"""Category controller - CRUD, hierarchy management, and pin/location membership.

Categories are Badge rows with kind='category'. They are user-owned - every
category belongs to a specific profile (no global/shared categories).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, Badge
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.queryset import BadgeQuerySet
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_CTX_BASE = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def _all_categories(profile: Profile) -> BadgeQuerySet:
    """Return categories belonging to *profile*, ordered for display with count annotations."""
    return Badge.objects.categories().for_profile(profile).ordered().with_pin_counts()


def _rows_ctx(profile: Profile, extra: dict | None = None) -> dict:
    """Build template context for category rows partial."""
    ctx = {**_CTX_BASE, "categories": _all_categories(profile)}
    if extra:
        ctx.update(extra)
    return ctx


class CategoryIndexView(LoginRequiredMixin, View):
    """Show all categories with create form, CRUD, and view toggle."""

    def get(self, request, *args, **kwargs):
        """Render the categories index page.

        Args:
            request: The HTTP request.

        Returns:
            Rendered categories/index.html.
        """
        return render(request, "dashboard/pages/categories/index.html", _rows_ctx(request.user.profile))


class CategoryCreateView(LoginRequiredMixin, View):
    """Create a new user-owned category (HTMX)."""

    def post(self, request, *args, **kwargs):
        """Create a category and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.

        Returns:
            Rendered category_rows.html partial.
        """
        profile = request.user.profile
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        description = request.POST.get("description", "").strip() or None
        icon = request.POST.get("icon") or None
        color = request.POST.get("color") or None
        order = int(request.POST.get("order", 0))
        parent_ids = request.POST.getlist("parent_ids")

        category = Badge.objects.create(
            kind="category",
            profile=profile,
            name=name,
            description=description,
            icon=icon,
            color=color,
            order=order,
        )
        if parent_ids:
            valid_parents = Badge.objects.visible_to(profile).filter(id__in=parent_ids).exclude(id=category.id)
            category.parents.set(valid_parents)

        return render(
            request,
            "dashboard/partials/category_rows.html",
            _rows_ctx(profile, {"new_category_id": category.id}),
        )


class CategoryEditView(LoginRequiredMixin, View):
    """Edit an existing user-owned category (HTMX)."""

    def get(self, request, cat_id, *args, **kwargs):
        """Return the edit form partial.

        Args:
            request: The HTTP request.
            cat_id: The category PK.

        Returns:
            Rendered category_edit_form.html partial.
        """
        profile = request.user.profile
        category = get_object_or_404(Badge, id=cat_id, kind="category", profile=profile)
        available_parents = Badge.objects.visible_to(profile).ordered().exclude(id=cat_id)
        parent_ids = set(category.parents.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/category_edit_form.html",
            {
                **_CTX_BASE,
                "category": category,
                "available_parents": available_parents,
                "parent_ids": parent_ids,
            },
        )

    def post(self, request, cat_id, *args, **kwargs):
        """Save edits and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.
            cat_id: The category PK.

        Returns:
            Rendered category_rows.html partial (X-Kind-Changed header set on conversion).
        """
        profile = request.user.profile
        category = get_object_or_404(Badge, id=cat_id, kind="category", profile=profile)

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        new_kind = request.POST.get("kind", "category")
        if new_kind not in {"tag", "category", "status"}:
            new_kind = "category"
        kind_changed = new_kind != category.kind

        category.name = name
        category.description = request.POST.get("description", "").strip() or None
        category.icon = request.POST.get("icon") or None
        category.color = request.POST.get("color") or None
        category.order = int(request.POST.get("order", category.order))

        if kind_changed and new_kind == "tag":
            category.kind = "tag"

        elif kind_changed and new_kind == "status":
            category.kind = "status"

        category.save()

        if kind_changed:
            category.parents.clear()
        else:
            parent_ids = request.POST.getlist("parent_ids")
            if parent_ids:
                valid_parents = Badge.objects.visible_to(profile).filter(id__in=parent_ids).exclude(id=cat_id)
                category.parents.set(valid_parents)
            else:
                category.parents.clear()

        response = render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))
        if kind_changed:
            response["X-Kind-Changed"] = new_kind
        return response


class CategoryDeleteView(LoginRequiredMixin, View):
    """Delete a user-owned category (HTMX). Pins and locations keep their other categories."""

    def post(self, request, cat_id, *args, **kwargs):
        """Delete the category and return the refreshed rows partial.

        Args:
            request: The HTTP request.
            cat_id: The category PK.

        Returns:
            Rendered category_rows.html partial.
        """
        profile = request.user.profile
        category = get_object_or_404(Badge, id=cat_id, kind="category", profile=profile)
        category.delete()
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


class CategoryReorderView(LoginRequiredMixin, View):
    """Persist a new drag-and-drop order for categories."""

    def post(self, request):
        """Save new category order from JSON body.

        Args:
            request: The HTTP request with JSON body containing category_ids list.

        Returns:
            JSON response with ok=True on success.
        """
        try:
            data = json.loads(request.body)
            cat_ids = [int(x) for x in data.get("category_ids", [])]
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        profile = request.user.profile
        total = len(cat_ids)
        for i, cat_id in enumerate(cat_ids):
            Badge.objects.filter(id=cat_id, kind="category", profile=profile).update(order=total - i)

        return JsonResponse({"ok": True})


class CategoryRowsView(LoginRequiredMixin, View):
    """Return the category-rows partial (used by cancel buttons)."""

    def get(self, request, *args, **kwargs):
        """Render the category rows partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered category_rows.html partial.
        """
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile))


class CategoryBulkDeleteView(LoginRequiredMixin, View):
    """Bulk-delete user-owned categories (JSON POST). Pins and locations keep their other categories."""

    def post(self, request, *args, **kwargs):
        """Delete the specified categories and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            Rendered category_rows.html partial.
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        profile = request.user.profile
        Badge.objects.filter(id__in=ids, kind="category", profile=profile).delete()
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


class CategoryBulkEditView(LoginRequiredMixin, View):
    """Bulk-edit icon, color, and/or parents for multiple user-owned categories (JSON POST).

    Key-absent = no change; null/empty string = clear; string value = set.
    add_parent_ids is additive - existing parents are never removed.
    """

    def post(self, request, *args, **kwargs):
        """Apply bulk edits and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body.

        Returns:
            Rendered category_rows.html partial.
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        profile = request.user.profile
        has_icon = "icon" in data
        has_color = "color" in data
        icon = data.get("icon") or None
        color = data.get("color") or None
        add_parent_ids = [int(x) for x in data.get("add_parent_ids", [])]

        categories = list(Badge.objects.filter(id__in=ids, kind="category", profile=profile))
        for cat in categories:
            update_fields = []
            if has_icon:
                cat.icon = icon
                update_fields.append("icon")
            if has_color:
                cat.color = color
                update_fields.append("color")
            if update_fields:
                cat.save(update_fields=update_fields)

        if add_parent_ids:
            valid_parents = list(Badge.objects.visible_to(profile).filter(id__in=add_parent_ids))
            for cat in categories:
                cat.parents.add(*[p for p in valid_parents if p.id != cat.id])

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


class CategoryBulkConvertView(LoginRequiredMixin, View):
    """Convert multiple user-owned categories to tags (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Convert categories to tags, migrating all pin and location memberships.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            Rendered category_rows.html partial (converted categories will be absent).
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        profile = request.user.profile
        cats_to_convert = list(Badge.objects.filter(id__in=ids, kind="category", profile=profile))
        for category in cats_to_convert:
            category.kind = "tag"
            category.parents.clear()
            category.save()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


class CategoryBulkConvertToStatusView(LoginRequiredMixin, View):
    """Convert multiple user-owned categories to personal status badges (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Convert categories to statuses, migrating pin memberships.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            Rendered category_rows.html partial (converted categories will be absent).
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        profile = request.user.profile
        cats_to_convert = list(Badge.objects.filter(id__in=ids, kind="category", profile=profile))
        for category in cats_to_convert:
            category.kind = "status"
            category.parents.clear()
            category.save()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


class CategoryMultiMergeView(LoginRequiredMixin, View):
    """Merge multiple user-owned categories into a single target (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Merge source categories into the target, then delete sources.

        Args:
            request: The HTTP request with JSON body containing target_id and source_ids.

        Returns:
            Rendered category_rows.html partial on success, or an error response.
        """
        try:
            data = json.loads(request.body)
            target_id = int(data.get("target_id", 0))
            source_ids = [int(x) for x in data.get("source_ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not target_id:
            return HttpResponse("target_id is required.", status=400)
        if not source_ids:
            return HttpResponse("At least one source_id is required.", status=400)

        profile = request.user.profile
        target = get_object_or_404(Badge, id=target_id, kind="category", profile=profile)
        sources = Badge.objects.filter(id__in=source_ids, kind="category", profile=profile).exclude(id=target_id)
        if not sources.exists():
            return HttpResponse("No valid source categories.", status=400)

        for source in sources:
            target.pins.add(*source.pins.all())
            target.locations.add(*source.locations.all())
            source.delete()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


class CategoryMergeView(LoginRequiredMixin, View):
    """Merge one category into another, transferring all pins and locations."""

    def get(self, request, cat_id, *args, **kwargs):
        """Return the merge-confirmation form.

        Args:
            request: The HTTP request.
            cat_id: The source category PK.

        Returns:
            Rendered category_merge_form.html partial.
        """
        profile = request.user.profile
        category = get_object_or_404(Badge, id=cat_id, kind="category", profile=profile)
        candidates = Badge.objects.categories().for_profile(profile).ordered().exclude(id=cat_id)
        return render(
            request,
            "dashboard/partials/category_merge_form.html",
            {
                "category": category,
                "candidates": candidates,
            },
        )

    def post(self, request, cat_id, *args, **kwargs):
        """Perform the merge: move all pins/locations to target, delete source.

        Args:
            request: The HTTP request with POST data.
            cat_id: The source category PK.

        Returns:
            Rendered category_rows.html partial.
        """
        profile = request.user.profile
        source = get_object_or_404(Badge, id=cat_id, kind="category", profile=profile)

        target_id = request.POST.get("target_category_id", "").strip()
        if not target_id:
            return HttpResponse("Target category is required.", status=400)

        target = get_object_or_404(Badge, id=target_id, kind="category", profile=profile)
        if target.id == source.id:
            return HttpResponse("Cannot merge a category into itself.", status=400)

        target.pins.add(*source.pins.all())
        target.locations.add(*source.locations.all())
        source.delete()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))


def _all_badges(profile: Profile) -> BadgeQuerySet:
    """Return all tag/category/status badges visible to *profile*, ordered for the badge picker."""
    return Badge.objects.visible_to(profile).ordered()


def _pin_member_ids(pin: Pin) -> set[int]:
    """Return the set of all badge IDs currently assigned to a pin."""
    return set(pin.badges.values_list("id", flat=True))


def _wiki_member_ids(wiki) -> set[int]:
    """Return the set of all badge IDs currently assigned to a community wiki."""
    return set(wiki.badges.values_list("id", flat=True))


def _apply_badge_to_pin(pin: Pin, badge: Badge, action: str) -> None:
    """Add or remove a badge from a pin."""
    if action == "add":
        pin.badges.add(badge)
    elif action == "remove":
        pin.badges.remove(badge)


def _apply_badge_to_wiki(wiki, badge: Badge, action: str) -> None:
    """Add or remove a badge from a community wiki."""
    if action == "add":
        wiki.badges.add(badge)
    elif action == "remove":
        wiki.badges.remove(badge)


def _resolve_wiki(location_slug: str):
    from urbanlens.dashboard.models.wiki.model import Wiki

    location = get_object_or_404(Location, slug=location_slug)
    wiki, _created = Wiki.objects.get_or_create_for_location(location)
    return location, wiki


class CategoryPinMembershipView(LoginRequiredMixin, View):
    """Add or remove a badge from a pin (HTMX panel on pin detail page)."""

    def get(self, request, pin_slug, *args, **kwargs):
        """Render the badge panel for a pin.

        Args:
            request: The HTTP request.
            pin_slug: The pin UUID.

        Returns:
            Rendered category_panel.html partial.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = request.user.profile
        return render(
            request,
            "dashboard/partials/category_panel.html",
            {
                "pin": pin,
                "all_categories": _all_badges(profile),
                "member_ids": _pin_member_ids(pin),
            },
        )

    def post(self, request, pin_slug, *args, **kwargs):
        """Add or remove a badge from a pin.

        Args:
            request: The HTTP request with POST data (category_id, action).
            pin_slug: The pin UUID.

        Returns:
            Rendered category_panel.html partial.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = request.user.profile
        badge_id = request.POST.get("category_id")
        action = request.POST.get("action")
        badge = get_object_or_404(Badge, id=badge_id, kind__in=["tag", "category", "status"])
        _apply_badge_to_pin(pin, badge, action)
        return render(
            request,
            "dashboard/partials/category_panel.html",
            {
                "pin": pin,
                "all_categories": _all_badges(profile),
                "member_ids": _pin_member_ids(pin),
            },
        )


class CategoryLocationMembershipView(LoginRequiredMixin, View):
    """Add or remove a badge from a location (HTMX panel on wiki page)."""

    def get(self, request, location_slug, *args, **kwargs):
        """Render the badge panel for a location.

        Args:
            request: The HTTP request.
            location_slug: The location UUID.

        Returns:
            Rendered category_location_panel.html partial.
        """
        location, wiki = _resolve_wiki(location_slug)
        profile = request.user.profile
        return render(
            request,
            "dashboard/partials/category_location_panel.html",
            {
                "location": location,
                "wiki": wiki,
                "all_categories": _all_badges(profile),
                "member_ids": _wiki_member_ids(wiki),
            },
        )

    def post(self, request, location_slug, *args, **kwargs):
        """Add or remove a badge from a community wiki.

        Args:
            request: The HTTP request with POST data (category_id, action).
            location_slug: The location UUID.

        Returns:
            Rendered category_location_panel.html partial.
        """
        location, wiki = _resolve_wiki(location_slug)
        profile = request.user.profile
        badge_id = request.POST.get("category_id")
        action = request.POST.get("action")
        badge = get_object_or_404(Badge, id=badge_id, kind__in=["tag", "category", "status"])
        _apply_badge_to_wiki(wiki, badge, action)
        return render(
            request,
            "dashboard/partials/category_location_panel.html",
            {
                "location": location,
                "wiki": wiki,
                "all_categories": _all_badges(profile),
                "member_ids": _wiki_member_ids(wiki),
            },
        )


class CategoryCustomizeView(LoginRequiredMixin, View):
    """Redirect customize requests to the standard edit view.

    CategoryCustomize was used for per-user overrides of global categories.
    Now that all categories are user-owned, edit directly.
    """

    def get(self, request, cat_id, *args, **kwargs):
        """Return the standard edit form partial.

        Args:
            request: The HTTP request.
            cat_id: The category PK.

        Returns:
            Rendered category_edit_form.html partial.
        """
        profile = request.user.profile
        category = get_object_or_404(Badge, id=cat_id, kind="category", profile=profile)
        available_parents = Badge.objects.visible_to(profile).ordered().exclude(id=cat_id)
        parent_ids = set(category.parents.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/category_edit_form.html",
            {
                **_CTX_BASE,
                "category": category,
                "available_parents": available_parents,
                "parent_ids": parent_ids,
            },
        )

    def post(self, request, cat_id, *args, **kwargs):
        """Delegate to CategoryEditView.post.

        Args:
            request: The HTTP request with POST data.
            cat_id: The category PK.

        Returns:
            Rendered category_rows.html partial.
        """
        return CategoryEditView().post(request, cat_id)
