"""Tag controller - CRUD and pin membership management."""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.tags.model import COLOR_CHOICES, ICON_CHOICES, Tag

logger = logging.getLogger(__name__)


class TagIndexView(LoginRequiredMixin, View):
    """Show all tags visible to the current user (global + their own)."""

    def get(self, request, *args, **kwargs):
        profile = request.user.profile
        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins")
        return render(request, "dashboard/pages/tags/index.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
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

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
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

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
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

        tags = Tag.objects.visible_to(profile).ordered().prefetch_related("pins")
        return render(request, "dashboard/partials/tag_rows.html", {
            "tags": tags,
            "icon_choices": ICON_CHOICES,
            "color_choices": COLOR_CHOICES,
        })


class TagMembershipView(LoginRequiredMixin, View):
    """Add or remove a tag from a specific pin (HTMX panel on pin detail page)."""

    def get(self, request, pin_id, *args, **kwargs):
        pin = get_object_or_404(Pin, id=pin_id, profile__user=request.user)
        profile = request.user.profile
        all_tags = Tag.objects.visible_to(profile).ordered()
        member_ids = set(pin.tags.values_list("id", flat=True))
        return render(request, "dashboard/partials/tag_panel.html", {
            "pin": pin,
            "all_tags": all_tags,
            "member_ids": member_ids,
        })

    def post(self, request, pin_id, *args, **kwargs):
        pin = get_object_or_404(Pin, id=pin_id, profile__user=request.user)
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
