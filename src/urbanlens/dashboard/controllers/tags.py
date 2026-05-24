"""Tag controller - CRUD and pin membership management."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.tags.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, Tag

logger = logging.getLogger(__name__)


class TagIndexView(LoginRequiredMixin, View):
    """Show all tags visible to the current user (global + their own)."""

    def get(self, request, *args, **kwargs):
        profile = request.user.profile
        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(request, "dashboard/pages/tags/index.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
        })


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
        order = int(request.POST.get("order", 0))
        parent_ids = request.POST.getlist("parent_ids")

        tag = Tag.objects.create(
            profile=profile,
            name=name,
            description=description,
            icon=icon,
            color=color,
            custom_icon=custom_icon,
            order=order,
        )
        if parent_ids:
            valid_parents = Tag.objects.filter(id__in=parent_ids).visible_to(profile)
            tag.parents.set(valid_parents)

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
            "new_tag_id": tag.id,
        })


class TagEditView(LoginRequiredMixin, View):
    """Edit an existing tag (HTMX)."""

    def get(self, request, tag_id, *args, **kwargs):
        tag = get_object_or_404(Tag, id=tag_id)
        if tag.profile is not None and tag.profile.user != request.user:
            return HttpResponseForbidden()
        profile = request.user.profile
        available_parents = Tag.objects.visible_to(profile).ordered().exclude(id=tag_id)
        parent_ids = set(tag.parents.values_list("id", flat=True))
        return render(request, "dashboard/partials/tag_edit_form.html", {
            "tag": tag,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
            "available_parents": available_parents,
            "parent_ids": parent_ids,
        })

    def post(self, request, tag_id, *args, **kwargs):
        tag = get_object_or_404(Tag, id=tag_id)
        if tag.profile is not None and tag.profile.user != request.user:
            return HttpResponseForbidden()

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        tag.name = name
        tag.description = request.POST.get("description", "").strip() or None
        tag.icon = request.POST.get("icon") or None
        tag.color = request.POST.get("color") or None
        tag.order = int(request.POST.get("order", tag.order))

        custom_icon = request.FILES.get("custom_icon")
        if custom_icon:
            tag.custom_icon = custom_icon
        elif request.POST.get("clear_custom_icon"):
            tag.custom_icon = None

        tag.save()

        parent_ids = request.POST.getlist("parent_ids")
        profile = request.user.profile
        if parent_ids:
            valid_parents = Tag.objects.filter(id__in=parent_ids).visible_to(profile).exclude(id=tag_id)
            tag.parents.set(valid_parents)
        else:
            tag.parents.clear()

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
        })


class TagDeleteView(LoginRequiredMixin, View):
    """Delete a user-owned tag (HTMX). Pins keep their other tags."""

    def post(self, request, tag_id, *args, **kwargs):
        tag = get_object_or_404(Tag, id=tag_id)
        if tag.profile is None or tag.profile.user != request.user:
            return HttpResponseForbidden()

        profile = tag.profile
        tag.delete()

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
        })


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
            Tag.objects.filter(id=tag_id, profile=profile).update(order=total - i)

        return JsonResponse({"ok": True})


class TagRowsView(LoginRequiredMixin, View):
    """Return the tag-rows partial (used by cancel buttons in inline forms)."""

    def get(self, request, *args, **kwargs):
        profile = request.user.profile
        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
        })


class TagMergeView(LoginRequiredMixin, View):
    """Merge one user-owned tag into another, transferring all pin memberships."""

    def get(self, request, tag_id, *args, **kwargs):
        """Return the merge-confirmation form for the given tag."""
        tag = get_object_or_404(Tag, id=tag_id)
        if tag.profile is None or tag.profile.user != request.user:
            return HttpResponseForbidden()
        profile = request.user.profile
        candidates = Tag.objects.visible_to(profile).ordered().exclude(id=tag_id)
        return render(request, "dashboard/partials/tag_merge_form.html", {
            "tag": tag,
            "candidates": candidates,
        })

    def post(self, request, tag_id, *args, **kwargs):
        """Perform the merge: move all pins to the target tag, then delete source."""
        source = get_object_or_404(Tag, id=tag_id)
        if source.profile is None or source.profile.user != request.user:
            return HttpResponseForbidden()

        target_id = request.POST.get("target_tag_id", "").strip()
        if not target_id:
            return HttpResponse("Target tag is required.", status=400)

        profile = request.user.profile
        target = get_object_or_404(Tag.objects.visible_to(profile), id=target_id)
        if target.id == source.id:
            return HttpResponse("Cannot merge a tag into itself.", status=400)

        # Transfer all pins from source → target in one bulk operation, then remove source.
        target.pins.add(*source.pins.all())
        source.delete()

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins", "children", "children__pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "icon_categories": ICON_CATEGORIES,
            "color_choices": COLOR_CHOICES,
        })


class TagMembershipView(LoginRequiredMixin, View):
    """Add or remove a tag from a specific pin (HTMX panel on pin detail page)."""

    def get(self, request, pin_uuid, *args, **kwargs):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        profile = request.user.profile
        all_tags = Tag.objects.visible_to(profile).ordered()
        member_ids = set(pin.tags.values_list("id", flat=True))
        return render(request, "dashboard/partials/tag_panel.html", {
            "pin": pin,
            "all_tags": all_tags,
            "member_ids": member_ids,
        })

    def post(self, request, pin_uuid, *args, **kwargs):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        tag_id = request.POST.get("tag_id")
        action = request.POST.get("action")  # "add" or "remove"

        profile = request.user.profile
        tag = get_object_or_404(Tag.objects.visible_to(profile), id=tag_id)

        if action == "add":
            pin.tags.add(tag)
        elif action == "remove":
            pin.tags.remove(tag)

        all_tags = Tag.objects.visible_to(profile).ordered()
        member_ids = set(pin.tags.values_list("id", flat=True))
        return render(request, "dashboard/partials/tag_panel.html", {
            "pin": pin,
            "all_tags": all_tags,
            "member_ids": member_ids,
        })
