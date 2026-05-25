"""Category controller - CRUD, hierarchy management, and pin/location membership."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.categories.model import Category
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.tags.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES

logger = logging.getLogger(__name__)

_CTX_BASE = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def _all_categories():
    """Return all categories ordered for display with common prefetches."""
    return Category.objects.ordered().prefetch_related("pins", "locations", "children", "children__pins")


def _rows_ctx(extra: dict | None = None) -> dict:
    ctx = {**_CTX_BASE, "categories": _all_categories()}
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
        return render(request, "dashboard/pages/categories/index.html", _rows_ctx())


class CategoryCreateView(LoginRequiredMixin, View):
    """Create a new global category (HTMX)."""

    def post(self, request, *args, **kwargs):
        """Create a category and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.

        Returns:
            Rendered category_rows.html partial.
        """
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        description = request.POST.get("description", "").strip() or None
        icon = request.POST.get("icon") or None
        color = request.POST.get("color") or None
        order = int(request.POST.get("order", 0))
        parent_ids = request.POST.getlist("parent_ids")

        category = Category.objects.create(
            name=name,
            description=description,
            icon=icon,
            color=color,
            order=order,
        )
        if parent_ids:
            valid_parents = Category.objects.filter(id__in=parent_ids)
            category.parents.set(valid_parents)

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx({"new_category_id": category.id}))


class CategoryEditView(LoginRequiredMixin, View):
    """Edit an existing category (HTMX)."""

    def get(self, request, cat_id, *args, **kwargs):
        """Return the edit form partial.

        Args:
            request: The HTTP request.
            cat_id: The category PK.

        Returns:
            Rendered category_edit_form.html partial.
        """
        category = get_object_or_404(Category, id=cat_id)
        available_parents = Category.objects.ordered().exclude(id=cat_id)
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
            Rendered category_rows.html partial.
        """
        category = get_object_or_404(Category, id=cat_id)

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        category.name = name
        category.description = request.POST.get("description", "").strip() or None
        category.icon = request.POST.get("icon") or None
        category.color = request.POST.get("color") or None
        category.order = int(request.POST.get("order", category.order))
        category.save()

        parent_ids = request.POST.getlist("parent_ids")
        if parent_ids:
            valid_parents = Category.objects.filter(id__in=parent_ids).exclude(id=cat_id)
            category.parents.set(valid_parents)
        else:
            category.parents.clear()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx())


class CategoryDeleteView(LoginRequiredMixin, View):
    """Delete a category (HTMX). Pins and locations keep their other categories."""

    def post(self, request, cat_id, *args, **kwargs):
        """Delete the category and return the refreshed rows partial.

        Args:
            request: The HTTP request.
            cat_id: The category PK.

        Returns:
            Rendered category_rows.html partial.
        """
        category = get_object_or_404(Category, id=cat_id)
        category.delete()
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx())


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

        total = len(cat_ids)
        for i, cat_id in enumerate(cat_ids):
            Category.objects.filter(id=cat_id).update(order=total - i)

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
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx())


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
        category = get_object_or_404(Category, id=cat_id)
        candidates = Category.objects.ordered().exclude(id=cat_id)
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
        source = get_object_or_404(Category, id=cat_id)

        target_id = request.POST.get("target_category_id", "").strip()
        if not target_id:
            return HttpResponse("Target category is required.", status=400)

        target = get_object_or_404(Category, id=target_id)
        if target.id == source.id:
            return HttpResponse("Cannot merge a category into itself.", status=400)

        target.pins.add(*source.pins.all())
        target.locations.add(*source.locations.all())
        source.delete()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx())


class CategoryPinMembershipView(LoginRequiredMixin, View):
    """Add or remove a category from a pin (HTMX panel on pin detail page)."""

    def get(self, request, pin_uuid, *args, **kwargs):
        """Render the category panel for a pin.

        Args:
            request: The HTTP request.
            pin_uuid: The pin UUID.

        Returns:
            Rendered category_panel.html partial.
        """
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        all_categories = Category.objects.ordered()
        member_ids = set(pin.categories.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/category_panel.html",
            {
                "pin": pin,
                "all_categories": all_categories,
                "member_ids": member_ids,
            },
        )

    def post(self, request, pin_uuid, *args, **kwargs):
        """Add or remove a category from a pin.

        Args:
            request: The HTTP request with POST data (category_id, action).
            pin_uuid: The pin UUID.

        Returns:
            Rendered category_panel.html partial.
        """
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        cat_id = request.POST.get("category_id")
        action = request.POST.get("action")

        category = get_object_or_404(Category, id=cat_id)
        if action == "add":
            pin.categories.add(category)
        elif action == "remove":
            pin.categories.remove(category)

        all_categories = Category.objects.ordered()
        member_ids = set(pin.categories.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/category_panel.html",
            {
                "pin": pin,
                "all_categories": all_categories,
                "member_ids": member_ids,
            },
        )


class CategoryLocationMembershipView(LoginRequiredMixin, View):
    """Add or remove a category from a location (HTMX panel on wiki page)."""

    def get(self, request, location_uuid, *args, **kwargs):
        """Render the category panel for a location.

        Args:
            request: The HTTP request.
            location_uuid: The location UUID.

        Returns:
            Rendered category_location_panel.html partial.
        """
        location = get_object_or_404(Location, uuid=location_uuid)
        all_categories = Category.objects.ordered()
        member_ids = set(location.categories.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/category_location_panel.html",
            {
                "location": location,
                "all_categories": all_categories,
                "member_ids": member_ids,
            },
        )

    def post(self, request, location_uuid, *args, **kwargs):
        """Add or remove a category from a location.

        Args:
            request: The HTTP request with POST data (category_id, action).
            location_uuid: The location UUID.

        Returns:
            Rendered category_location_panel.html partial.
        """
        location = get_object_or_404(Location, uuid=location_uuid)
        cat_id = request.POST.get("category_id")
        action = request.POST.get("action")

        category = get_object_or_404(Category, id=cat_id)
        if action == "add":
            location.categories.add(category)
        elif action == "remove":
            location.categories.remove(category)

        all_categories = Category.objects.ordered()
        member_ids = set(location.categories.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/category_location_panel.html",
            {
                "location": location,
                "all_categories": all_categories,
                "member_ids": member_ids,
            },
        )
