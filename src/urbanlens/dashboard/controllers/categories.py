"""Category controller - CRUD, hierarchy management, and pin/location membership.

Categories are Badge rows with kind='category'.
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, Badge
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

_CTX_BASE = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def _all_categories(profile=None):
    """Return all category-kind tags ordered for display with count annotations."""
    qs = Badge.objects.categories().ordered().with_pin_counts()
    if profile is not None:
        qs = qs.with_customizations_for(profile)
    return qs


_PERM = "dashboard.edit_global_badge"


def _rows_ctx(profile=None, can_edit_global: bool = False, extra: dict | None = None) -> dict:
    ctx = {**_CTX_BASE, "categories": _all_categories(profile), "can_edit_global": can_edit_global}
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
        return render(request, "dashboard/pages/categories/index.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


class CategoryCreateView(LoginRequiredMixin, View):
    """Create a new global category (HTMX)."""

    def post(self, request, *args, **kwargs):
        """Create a category and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.

        Returns:
            Rendered category_rows.html partial.
        """
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()

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
            profile=None,
            name=name,
            description=description,
            icon=icon,
            color=color,
            order=order,
        )
        if parent_ids:
            valid_parents = Badge.objects.categories().filter(id__in=parent_ids)
            category.parents.set(valid_parents)

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM), {"new_category_id": category.id}))


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
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        category = get_object_or_404(Badge, id=cat_id, kind="category")
        available_parents = Badge.objects.categories().ordered().exclude(id=cat_id)
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
            Rendered category_rows.html partial, or an HX-Redirect on kind conversion.
        """
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        category = get_object_or_404(Badge, id=cat_id, kind="category")

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        new_kind = request.POST.get("kind", "category")
        kind_changed = new_kind != category.kind

        category.name = name
        category.description = request.POST.get("description", "").strip() or None
        category.icon = request.POST.get("icon") or None
        category.color = request.POST.get("color") or None
        category.order = int(request.POST.get("order", category.order))

        if kind_changed and new_kind == "tag":
            # Migrate category → tag: move pin.categories → pin.tags and
            # location.categories → location.tags.
            profile = request.user.profile
            for pin in Pin.objects.filter(categories=category):
                pin.tags.add(category)
                pin.categories.remove(category)
            for loc in Location.objects.filter(categories=category):
                loc.tags.add(category)
                loc.categories.remove(category)
            category.kind = "tag"
            category.profile = profile

        category.save()

        if kind_changed:
            # Parent IDs from the form belong to the old kind — clear and let the user re-set them.
            category.parents.clear()
        else:
            parent_ids = request.POST.getlist("parent_ids")
            if parent_ids:
                valid_parents = Badge.objects.categories().filter(id__in=parent_ids).exclude(id=cat_id)
                category.parents.set(valid_parents)
            else:
                category.parents.clear()

        if kind_changed and new_kind == "tag":
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("organize.index") + "?tab=tags"
            return response

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


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
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        category = get_object_or_404(Badge, id=cat_id, kind="category")
        category.delete()
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


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
            Badge.objects.filter(id=cat_id, kind="category").update(order=total - i)

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
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


class CategoryBulkDeleteView(LoginRequiredMixin, View):
    """Bulk-delete categories (JSON POST). Pins and locations keep their other categories."""

    def post(self, request, *args, **kwargs):
        """Delete the specified categories and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            Rendered category_rows.html partial.
        """
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        Badge.objects.filter(id__in=ids, kind="category").delete()
        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


class CategoryBulkEditView(LoginRequiredMixin, View):
    """Bulk-edit icon, color, and/or parents for multiple categories (JSON POST).

    Key-absent = no change; null/empty string = clear; string value = set.
    add_parent_ids is additive — existing parents are never removed.
    """

    def post(self, request, *args, **kwargs):
        """Apply bulk edits and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body.

        Returns:
            Rendered category_rows.html partial.
        """
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        has_icon = "icon" in data
        has_color = "color" in data
        icon = data.get("icon") or None
        color = data.get("color") or None
        add_parent_ids = [int(x) for x in data.get("add_parent_ids", [])]

        categories = list(Badge.objects.filter(id__in=ids, kind="category"))
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
            valid_parents = list(Badge.objects.categories().filter(id__in=add_parent_ids))
            for cat in categories:
                cat.parents.add(*[p for p in valid_parents if p.id != cat.id])

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


class CategoryBulkConvertView(LoginRequiredMixin, View):
    """Convert multiple categories to user-owned tags (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Convert categories to tags, migrating all pin and location memberships.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            204 with HX-Redirect header pointing to the tags tab.
        """
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No categories specified.", status=400)

        profile = request.user.profile
        cats_to_convert = list(Badge.objects.filter(id__in=ids, kind="category"))
        for category in cats_to_convert:
            for pin in Pin.objects.filter(categories=category):
                pin.tags.add(category)
                pin.categories.remove(category)
            for loc in Location.objects.filter(categories=category):
                loc.tags.add(category)
                loc.categories.remove(category)
            category.kind = "tag"
            category.profile = profile
            category.parents.clear()
            category.save()

        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("organize.index") + "?tab=tags"
        return response


class CategoryMultiMergeView(LoginRequiredMixin, View):
    """Merge multiple categories into a single target (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Merge source categories into the target, then delete sources.

        Args:
            request: The HTTP request with JSON body containing target_id and source_ids.

        Returns:
            Rendered category_rows.html partial on success, or an error response.
        """
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
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

        target = get_object_or_404(Badge, id=target_id, kind="category")
        sources = Badge.objects.filter(id__in=source_ids, kind="category").exclude(id=target_id)
        if not sources.exists():
            return HttpResponse("No valid source categories.", status=400)

        for source in sources:
            target.categorized_pins.add(*source.categorized_pins.all())
            target.categorized_locations.add(*source.categorized_locations.all())
            source.delete()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


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
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        category = get_object_or_404(Badge, id=cat_id, kind="category")
        candidates = Badge.objects.categories().ordered().exclude(id=cat_id)
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
        if not request.user.has_perm(_PERM):
            return HttpResponseForbidden()
        source = get_object_or_404(Badge, id=cat_id, kind="category")

        target_id = request.POST.get("target_category_id", "").strip()
        if not target_id:
            return HttpResponse("Target category is required.", status=400)

        target = get_object_or_404(Badge, id=target_id, kind="category")
        if target.id == source.id:
            return HttpResponse("Cannot merge a category into itself.", status=400)

        target.categorized_pins.add(*source.categorized_pins.all())
        target.categorized_locations.add(*source.categorized_locations.all())
        source.delete()

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(request.user.profile, request.user.has_perm(_PERM)))


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
        all_categories = Badge.objects.categories().ordered()
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

        category = get_object_or_404(Badge, id=cat_id, kind="category")
        if action == "add":
            pin.categories.add(category)
        elif action == "remove":
            pin.categories.remove(category)

        all_categories = Badge.objects.categories().ordered()
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
        all_categories = Badge.objects.categories().ordered()
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

        category = get_object_or_404(Badge, id=cat_id, kind="category")
        if action == "add":
            location.categories.add(category)
        elif action == "remove":
            location.categories.remove(category)

        all_categories = Badge.objects.categories().ordered()
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


class CategoryCustomizeView(LoginRequiredMixin, View):
    """Show and save per-user display overrides for a global category."""

    def get(self, request, cat_id, *args, **kwargs):
        """Render the customization form partial.

        Args:
            request: The HTTP request.
            cat_id: The category PK.

        Returns:
            Rendered category_customize_form.html partial.
        """
        category = get_object_or_404(Badge, id=cat_id, kind="category")
        profile = request.user.profile
        from urbanlens.dashboard.models.badges.customization import BadgeCustomization
        customization = BadgeCustomization.objects.filter(profile=profile, badge=category).first()
        return render(request, "dashboard/partials/category_customize_form.html", {
            **_CTX_BASE,
            "category": category,
            "customization": customization,
        })

    def post(self, request, cat_id, *args, **kwargs):
        """Save or clear the customization and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.
            cat_id: The category PK.

        Returns:
            Rendered category_rows.html partial.
        """
        category = get_object_or_404(Badge, id=cat_id, kind="category")
        profile = request.user.profile

        from urbanlens.dashboard.models.badges.customization import BadgeCustomization

        if request.POST.get("action") == "clear":
            BadgeCustomization.objects.filter(profile=profile, badge=category).delete()
        else:
            name = request.POST.get("name", "").strip() or None
            icon = request.POST.get("icon") or None
            color = request.POST.get("color") or None
            if name is None and icon is None and color is None:
                BadgeCustomization.objects.filter(profile=profile, badge=category).delete()
            else:
                BadgeCustomization.objects.update_or_create(
                    profile=profile,
                    badge=category,
                    defaults={"name": name, "icon": icon, "color": color},
                )

        return render(request, "dashboard/partials/category_rows.html", _rows_ctx(profile))
