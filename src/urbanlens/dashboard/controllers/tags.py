"""Tag controller - CRUD and pin membership management."""

from __future__ import annotations

import contextlib
import io
import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, Badge
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

_ICON_MAX_PX = 256


def _resize_custom_icon(uploaded_file):
    """Resize an uploaded icon to at most _ICON_MAX_PX * _ICON_MAX_PX pixels.

    Returns the original file unchanged if it is already within bounds or if
    Pillow cannot open it. Always rewinds the file before returning.
    """
    try:
        from django.core.files.uploadedfile import InMemoryUploadedFile
        from PIL import Image

        img = Image.open(uploaded_file)
        if max(img.width, img.height) <= _ICON_MAX_PX:
            uploaded_file.seek(0)
            return uploaded_file

        img = img.convert("RGBA") if img.mode in {"RGBA", "P", "PA"} else img.convert("RGB")
        img.thumbnail((_ICON_MAX_PX, _ICON_MAX_PX), Image.LANCZOS)
        fmt = "PNG" if img.mode == "RGBA" else "JPEG"
        out = io.BytesIO()
        img.save(out, format=fmt, quality=88, optimize=True)
        out.seek(0)
        name = uploaded_file.name or "icon"
        ext = ".png" if fmt == "PNG" else ".jpg"
        if not name.lower().endswith(ext):
            name = name.rsplit(".", 1)[0] + ext
        return InMemoryUploadedFile(out, "ImageField", name, f"image/{fmt.lower()}", out.getbuffer().nbytes, None)
    except Exception:
        with contextlib.suppress(Exception):
            uploaded_file.seek(0)
        return uploaded_file


_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


_PERM = "dashboard.edit_global_badge"


def _rows_ctx(profile, can_edit_global: bool = False, extra: dict | None = None) -> dict:
    tags = (
        Badge.objects.tags()
        .visible_to(profile)
        .ordered()
        .with_customizations_for(profile)
        .prefetch_related("pins", "children", "children__pins")
    )
    ctx = {**_BASE_CTX, "tags": tags, "can_edit_global": can_edit_global}
    if extra:
        ctx.update(extra)
    return ctx


class TagIndexView(LoginRequiredMixin, View):
    """Show all tags visible to the current user (global + their own)."""

    def get(self, request, *args, **kwargs):
        profile = request.user.profile
        return render(request, "dashboard/pages/tags/index.html", _rows_ctx(profile, request.user.has_perm(_PERM)))


class TagCreateView(LoginRequiredMixin, View):
    """Create a new user-specific tag (HTMX)."""

    def post(self, request, *args, **kwargs):
        profile = request.user.profile
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        description = request.POST.get("description", "").strip() or None
        icon = request.POST.get("icon") or None
        color = request.POST.get("color") or None
        custom_icon = request.FILES.get("custom_icon") or None
        if custom_icon:
            custom_icon = _resize_custom_icon(custom_icon)
        order = int(request.POST.get("order", 0))
        parent_ids = request.POST.getlist("parent_ids")

        tag = Badge.objects.create(
            kind="tag",
            profile=profile,
            name=name,
            description=description,
            icon=icon,
            color=color,
            custom_icon=custom_icon,
            order=order,
        )
        if parent_ids:
            valid_parents = Badge.objects.tags().filter(id__in=parent_ids).visible_to(profile)
            tag.parents.set(valid_parents)

        return render(
            request,
            "dashboard/partials/tag_rows.html",
            _rows_ctx(profile, request.user.has_perm(_PERM), {"new_tag_id": tag.id}),
        )


class TagEditView(LoginRequiredMixin, View):
    """Edit an existing tag (HTMX)."""

    def get(self, request, tag_id, *args, **kwargs):
        tag = get_object_or_404(Badge, id=tag_id, kind="tag")
        if tag.profile is None:
            if not request.user.has_perm(_PERM):
                return HttpResponseForbidden()
        elif tag.profile.user != request.user:
            return HttpResponseForbidden()
        profile = request.user.profile
        available_parents = Badge.objects.tags().visible_to(profile).ordered().exclude(id=tag_id)
        parent_ids = set(tag.parents.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/tag_edit_form.html",
            {
                "tag": tag,
                "icon_choices": ICON_CHOICES,
                "icon_categories": ICON_CATEGORIES,
                "color_choices": COLOR_CHOICES,
                "available_parents": available_parents,
                "parent_ids": parent_ids,
                "is_global": tag.profile is None,
            },
        )

    def post(self, request, tag_id, *args, **kwargs):
        tag = get_object_or_404(Badge, id=tag_id, kind="tag")
        if tag.profile is None:
            if not request.user.has_perm(_PERM):
                return HttpResponseForbidden()
        elif tag.profile.user != request.user:
            return HttpResponseForbidden()

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        new_kind = request.POST.get("kind", "tag")
        kind_changed = new_kind != tag.kind

        tag.name = name
        tag.description = request.POST.get("description", "").strip() or None
        tag.icon = request.POST.get("icon") or None
        tag.color = request.POST.get("color") or None
        tag.order = int(request.POST.get("order", tag.order))

        custom_icon = request.FILES.get("custom_icon")
        if custom_icon:
            tag.custom_icon = _resize_custom_icon(custom_icon)
        elif request.POST.get("clear_custom_icon"):
            tag.custom_icon = None

        if kind_changed and new_kind == "category":
            # Migrate tag → category: move pin.tags → pin.categories and
            # location.tags → location.categories, then make it a global category.
            from urbanlens.dashboard.models.location.model import Location
            from urbanlens.dashboard.models.pin.model import Pin

            for pin in Pin.objects.filter(tags=tag):
                pin.categories.add(tag)
                pin.tags.remove(tag)
            for loc in Location.objects.filter(tags=tag):
                loc.categories.add(tag)
                loc.tags.remove(tag)
            tag.kind = "category"
            tag.profile = None

        tag.save()

        profile = request.user.profile
        if kind_changed:
            # Parent IDs from the form belong to the old kind — clear and let the user re-set them.
            tag.parents.clear()
        else:
            parent_ids = request.POST.getlist("parent_ids")
            if parent_ids:
                valid_parents = Badge.objects.tags().filter(id__in=parent_ids).visible_to(profile).exclude(id=tag_id)
                tag.parents.set(valid_parents)
            else:
                tag.parents.clear()

        if kind_changed and new_kind == "category":
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("organize.index") + "?tab=categories"
            return response

        return render(request, "dashboard/partials/tag_rows.html", _rows_ctx(profile, request.user.has_perm(_PERM)))


class TagDeleteView(LoginRequiredMixin, View):
    """Delete a tag (HTMX). Pins keep their other tags."""

    def post(self, request, tag_id, *args, **kwargs):
        tag = get_object_or_404(Badge, id=tag_id, kind="tag")
        if tag.profile is None:
            if not request.user.has_perm(_PERM):
                return HttpResponseForbidden()
        elif tag.profile.user != request.user:
            return HttpResponseForbidden()

        tag.delete()
        profile = request.user.profile
        return render(request, "dashboard/partials/tag_rows.html", _rows_ctx(profile, request.user.has_perm(_PERM)))


class TagReorderView(LoginRequiredMixin, View):
    """Persist a new drag-and-drop order for the current user's tags."""

    def post(self, request):
        try:
            data = json.loads(request.body)
            tag_ids = [int(x) for x in data.get("tag_ids", [])]
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        profile = request.user.profile
        total = len(tag_ids)
        for i, tag_id in enumerate(tag_ids):
            # Assign descending values so top item gets the highest order,
            # matching the ordered() queryset which sorts by -order.
            Badge.objects.filter(id=tag_id, profile=profile, kind="tag").update(order=total - i)

        return JsonResponse({"ok": True})


class TagRowsView(LoginRequiredMixin, View):
    """Return the tag-rows partial (used by cancel buttons in inline forms)."""

    def get(self, request, *args, **kwargs):
        profile = request.user.profile
        tags = Badge.objects.tags().visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(
            request,
            "dashboard/partials/tag_rows.html",
            {
                "tags": tags,
                "icon_choices": ICON_CHOICES,
                "icon_categories": ICON_CATEGORIES,
                "color_choices": COLOR_CHOICES,
            },
        )


class TagMergeView(LoginRequiredMixin, View):
    """Merge one user-owned tag into another, transferring all pin memberships."""

    def get(self, request, tag_id, *args, **kwargs):
        """Return the merge-confirmation form for the given tag."""
        tag = get_object_or_404(Badge, id=tag_id, kind="tag")
        if tag.profile is None or tag.profile.user != request.user:
            return HttpResponseForbidden()
        profile = request.user.profile
        candidates = Badge.objects.tags().visible_to(profile).ordered().exclude(id=tag_id)
        return render(
            request,
            "dashboard/partials/tag_merge_form.html",
            {
                "tag": tag,
                "candidates": candidates,
            },
        )

    def post(self, request, tag_id, *args, **kwargs):
        """Perform the merge: move all pins to the target tag, then delete source."""
        source = get_object_or_404(Badge, id=tag_id, kind="tag")
        if source.profile is None or source.profile.user != request.user:
            return HttpResponseForbidden()

        target_id = request.POST.get("target_tag_id", "").strip()
        if not target_id:
            return HttpResponse("Target tag is required.", status=400)

        profile = request.user.profile
        target = get_object_or_404(Badge.objects.tags().visible_to(profile), id=target_id)
        if target.id == source.id:
            return HttpResponse("Cannot merge a tag into itself.", status=400)

        # Transfer all pins from source → target in one bulk operation, then remove source.
        target.pins.add(*source.pins.all())
        source.delete()

        tags = Badge.objects.tags().visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(
            request,
            "dashboard/partials/tag_rows.html",
            {
                "tags": tags,
                "icon_choices": ICON_CHOICES,
                "icon_categories": ICON_CATEGORIES,
                "color_choices": COLOR_CHOICES,
            },
        )


class TagMembershipView(LoginRequiredMixin, View):
    """Add or remove a tag from a specific pin (HTMX panel on pin detail page)."""

    def get(self, request, pin_uuid, *args, **kwargs):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        profile = request.user.profile
        all_tags = Badge.objects.tags().visible_to(profile).ordered()
        member_ids = set(pin.tags.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/tag_panel.html",
            {
                "pin": pin,
                "all_tags": all_tags,
                "member_ids": member_ids,
            },
        )

    def post(self, request, pin_uuid, *args, **kwargs):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        tag_id = request.POST.get("tag_id")
        action = request.POST.get("action")  # "add" or "remove"

        profile = request.user.profile
        tag = get_object_or_404(Badge.objects.tags().visible_to(profile), id=tag_id)

        if action == "add":
            pin.tags.add(tag)
        elif action == "remove":
            pin.tags.remove(tag)

        all_tags = Badge.objects.tags().visible_to(profile).ordered()
        member_ids = set(pin.tags.values_list("id", flat=True))
        return render(
            request,
            "dashboard/partials/tag_panel.html",
            {
                "pin": pin,
                "all_tags": all_tags,
                "member_ids": member_ids,
            },
        )


class TagBulkDeleteView(LoginRequiredMixin, View):
    """Bulk-delete user-owned tags (JSON POST). Pins keep their other tags."""

    def post(self, request, *args, **kwargs):
        """Delete the specified tags and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            Rendered tag_rows.html partial.
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No tags specified.", status=400)

        profile = request.user.profile
        Badge.objects.filter(id__in=ids, profile=profile, kind="tag").delete()
        return render(request, "dashboard/partials/tag_rows.html", _rows_ctx(profile, request.user.has_perm(_PERM)))


class TagBulkEditView(LoginRequiredMixin, View):
    """Bulk-edit icon, color, and/or parents for multiple user-owned tags (JSON POST).

    Key-absent = no change; null/empty string = clear; string value = set.
    add_parent_ids is additive — existing parents are never removed.
    """

    def post(self, request, *args, **kwargs):
        """Apply bulk edits and return the refreshed rows partial.

        Args:
            request: The HTTP request with JSON body.

        Returns:
            Rendered tag_rows.html partial.
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No tags specified.", status=400)

        profile = request.user.profile
        has_icon = "icon" in data
        has_color = "color" in data
        icon = data.get("icon") or None
        color = data.get("color") or None
        add_parent_ids = [int(x) for x in data.get("add_parent_ids", [])]

        tags = list(Badge.objects.filter(id__in=ids, profile=profile, kind="tag"))
        for tag in tags:
            update_fields = []
            if has_icon:
                tag.icon = icon
                update_fields.append("icon")
            if has_color:
                tag.color = color
                update_fields.append("color")
            if update_fields:
                tag.save(update_fields=update_fields)

        if add_parent_ids:
            valid_parents = list(Badge.objects.tags().filter(id__in=add_parent_ids).visible_to(profile))
            for tag in tags:
                tag.parents.add(*[p for p in valid_parents if p.id != tag.id])

        return render(request, "dashboard/partials/tag_rows.html", _rows_ctx(profile, request.user.has_perm(_PERM)))


class TagBulkConvertView(LoginRequiredMixin, View):
    """Convert multiple user-owned tags to global categories (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Convert tags to categories, migrating all pin and location memberships.

        Args:
            request: The HTTP request with JSON body containing ids list.

        Returns:
            204 with HX-Redirect header pointing to the categories tab.
        """
        try:
            data = json.loads(request.body)
            ids = [int(x) for x in data.get("ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not ids:
            return HttpResponse("No tags specified.", status=400)

        from urbanlens.dashboard.models.location.model import Location

        profile = request.user.profile
        tags_to_convert = list(Badge.objects.filter(id__in=ids, profile=profile, kind="tag"))
        for tag in tags_to_convert:
            for pin in Pin.objects.filter(tags=tag):
                pin.categories.add(tag)
                pin.tags.remove(tag)
            for loc in Location.objects.filter(tags=tag):
                loc.categories.add(tag)
                loc.tags.remove(tag)
            tag.kind = "category"
            tag.profile = None
            tag.parents.clear()
            tag.save()

        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("organize.index") + "?tab=categories"
        return response


class TagMultiMergeView(LoginRequiredMixin, View):
    """Merge multiple tags into a single target (JSON POST)."""

    def post(self, request, *args, **kwargs):
        """Merge source tags into the target, then delete sources.

        Args:
            request: The HTTP request with JSON body containing target_id and source_ids.

        Returns:
            Rendered tag_rows.html partial on success, or an error response.
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
        target = get_object_or_404(Badge.objects.tags().visible_to(profile), id=target_id)
        sources = Badge.objects.filter(id__in=source_ids, profile=profile, kind="tag").exclude(id=target_id)
        if not sources.exists():
            return HttpResponse("No valid source tags.", status=400)

        for source in sources:
            target.pins.add(*source.pins.all())
            source.delete()

        return render(request, "dashboard/partials/tag_rows.html", _rows_ctx(profile, request.user.has_perm(_PERM)))


class TagCustomizeView(LoginRequiredMixin, View):
    """Show and save per-user display overrides for a global tag."""

    def get(self, request, tag_id, *args, **kwargs):
        """Render the customization form partial.

        Args:
            request: The HTTP request.
            tag_id: The tag PK.

        Returns:
            Rendered tag_customize_form.html partial.
        """
        tag = get_object_or_404(Badge, id=tag_id, kind="tag")
        profile = request.user.profile
        from urbanlens.dashboard.models.badges.customization import BadgeCustomization

        customization = BadgeCustomization.objects.filter(profile=profile, badge=tag).first()
        return render(
            request,
            "dashboard/partials/tag_customize_form.html",
            {
                **_BASE_CTX,
                "tag": tag,
                "customization": customization,
            },
        )

    def post(self, request, tag_id, *args, **kwargs):
        """Save or clear the customization and return the refreshed rows partial.

        Args:
            request: The HTTP request with POST data.
            tag_id: The tag PK.

        Returns:
            Rendered tag_rows.html partial.
        """
        tag = get_object_or_404(Badge, id=tag_id)
        profile = request.user.profile

        from urbanlens.dashboard.models.badges.customization import BadgeCustomization

        if request.POST.get("action") == "clear":
            BadgeCustomization.objects.filter(profile=profile, badge=tag).delete()
        else:
            name = request.POST.get("name", "").strip() or None
            icon = request.POST.get("icon") or None
            color = request.POST.get("color") or None
            if name is None and icon is None and color is None:
                BadgeCustomization.objects.filter(profile=profile, badge=tag).delete()
            else:
                BadgeCustomization.objects.update_or_create(
                    profile=profile,
                    badge=tag,
                    defaults={"name": name, "icon": icon, "color": color},
                )

        return render(request, "dashboard/partials/tag_rows.html", _rows_ctx(profile, request.user.has_perm(_PERM)))
