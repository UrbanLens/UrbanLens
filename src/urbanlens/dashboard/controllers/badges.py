"""Unified badge controller for tag, category, status, and people label CRUD.

All organize badge kinds are ``Badge`` rows distinguished by ``kind``.
Views read ``badge_kind`` from the URL (see ``badge_urls.py``).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import io
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User as AuthUser
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.html import escape
from django.views import View

from urbanlens.dashboard.models.badges.model import (
    COLOR_CHOICES,
    ICON_CATEGORIES,
    ICON_CHOICES,
    KIND_CATEGORY,
    KIND_STATUS,
    KIND_TAG,
    KIND_USER,
    Badge,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def _request_profile(request: HttpRequest) -> Profile:
    """Return the authenticated user's Profile; raises if user is anonymous."""
    if not isinstance(request.user, AuthUser):
        raise TypeError("Expected an authenticated user")
    return request.user.profile


_PERM = "dashboard.edit_global_badge"
_ICON_MAX_PX = 256
_ORGANIZE_KINDS = frozenset({KIND_TAG, KIND_CATEGORY, KIND_STATUS})

# URL segment (tag/category/status) aliases → model kind constants.
URL_KIND_TO_MODEL: dict[str, str] = {
    "tag": KIND_TAG,
    "tags": KIND_TAG,
    "category": KIND_CATEGORY,
    "categories": KIND_CATEGORY,
    "status": KIND_STATUS,
    "statuses": KIND_STATUS,
    "people": KIND_USER,
}
MODEL_KIND_TO_URL: dict[str, str] = {
    KIND_TAG: "tag",
    KIND_CATEGORY: "category",
    KIND_STATUS: "status",
    KIND_USER: "people",
}

_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


@dataclass(frozen=True)
class _KindConfig:
    """Per-kind template and URL metadata for organize badge views."""

    kind: str
    url_kind: str
    display_kind: str
    singular_title: str
    rows_context_key: str
    rows_target: str
    select_class: str
    select_data_name: str
    empty_icon: str
    empty_message: str
    organize_tab: str
    standalone_title: str
    standalone_subtitle: str | None = None
    new_id_key: str | None = None
    show_location_count: bool = False
    show_kind_toggle: bool = True
    edit_target: str = "#badge-edit-dialog-body"
    enable_single_merge: bool = True


_KIND_CONFIG: dict[str, _KindConfig] = {
    KIND_TAG: _KindConfig(
        kind=KIND_TAG,
        url_kind="tag",
        display_kind="tag",
        singular_title="Tag",
        rows_context_key="tags",
        rows_target="#tag-rows",
        select_class="tag-select-cb",
        select_data_name="tag",
        empty_icon="label",
        empty_message="No tags yet. Create one to start organizing your pins.",
        organize_tab="tags",
        standalone_title="My Tags",
        standalone_subtitle="Organize your pins with custom tags.",
        new_id_key="new_tag_id",
    ),
    KIND_CATEGORY: _KindConfig(
        kind=KIND_CATEGORY,
        url_kind="category",
        display_kind="category",
        singular_title="Category",
        rows_context_key="categories",
        rows_target="#category-rows",
        select_class="cat-select-cb",
        select_data_name="cat",
        empty_icon="category",
        empty_message="No categories yet. Create one to start organizing your pins and locations.",
        organize_tab="categories",
        standalone_title="Categories",
        new_id_key="new_category_id",
        show_location_count=True,
    ),
    KIND_STATUS: _KindConfig(
        kind=KIND_STATUS,
        url_kind="status",
        display_kind="status",
        singular_title="Status",
        rows_context_key="statuses",
        rows_target="#status-rows",
        select_class="status-select-cb",
        select_data_name="status",
        empty_icon="flag",
        empty_message="No status labels yet. Create one to get started.",
        organize_tab="status",
        standalone_title="Statuses",
        standalone_subtitle="Track visit progress with status labels.",
        new_id_key="new_status_id",
    ),
    KIND_USER: _KindConfig(
        kind=KIND_USER,
        url_kind="people",
        display_kind="people",
        singular_title="Label",
        rows_context_key="user_badges",
        rows_target="#people-badge-rows",
        select_class="people-sel-cb",
        select_data_name="people",
        empty_icon="person",
        empty_message="No people labels yet. Create one to start organizing people.",
        organize_tab="people",
        standalone_title="People Labels",
        standalone_subtitle="Private labels for organizing people in your network.",
        show_kind_toggle=False,
        edit_target="#people-badge-edit-dialog-body",
        enable_single_merge=False,
    ),
}


def _config(kind: str) -> _KindConfig:
    """Return configuration for an organize badge kind.

    Args:
        kind: Badge kind string (tag, category, status, or user).

    Returns:
        Frozen config for the kind.

    Raises:
        KeyError: If kind is not a supported organize badge kind.
    """
    return _KIND_CONFIG[kind]


def _kind_from_url(url_kind: str) -> str | None:
    """Map a URL ``badge_kind`` segment to a model kind constant."""
    return URL_KIND_TO_MODEL.get(url_kind)


def _badge_id_from_kwargs(kwargs: dict[str, Any]) -> int:
    """Extract a badge PK from URL kwargs.

    Args:
        kwargs: URL keyword arguments from the view.

    Returns:
        Integer badge primary key.

    Raises:
        KeyError: If no badge id is present.
    """
    if "badge_id" in kwargs:
        return int(kwargs["badge_id"])
    for key in ("tag_id", "cat_id", "status_id"):
        if key in kwargs:
            return int(kwargs[key])
    msg = "No badge id in URL kwargs"
    raise KeyError(msg)


def _resize_custom_icon(uploaded_file: UploadedFile) -> UploadedFile:
    """Resize an uploaded icon to at most _ICON_MAX_PX pixels per side.

    Args:
        uploaded_file: Uploaded image file.

    Returns:
        Resized file, or the original if already small enough or unreadable.
    """
    try:
        from django.core.files.uploadedfile import InMemoryUploadedFile
        from PIL import Image

        img: Image.Image = Image.open(uploaded_file)
        if max(img.width, img.height) <= _ICON_MAX_PX:
            uploaded_file.seek(0)
            return uploaded_file

        img = img.convert("RGBA") if img.mode in {"RGBA", "P", "PA"} else img.convert("RGB")
        img.thumbnail((_ICON_MAX_PX, _ICON_MAX_PX), Image.Resampling.LANCZOS)
        fmt = "PNG" if img.mode == "RGBA" else "JPEG"
        out = io.BytesIO()
        img.save(out, format=fmt, quality=88, optimize=True)
        out.seek(0)
        name = uploaded_file.name or "icon"
        ext = ".png" if fmt == "PNG" else ".jpg"
        if not name.lower().endswith(ext):
            name = name.rsplit(".", 1)[0] + ext
        return InMemoryUploadedFile(out, "ImageField", name, f"image/{fmt.lower()}", out.getbuffer().nbytes, None)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            uploaded_file.seek(0)
        return uploaded_file


def _queryset_for_kind(kind: str, profile: Profile) -> QuerySet[Badge]:
    """Return the display queryset for a badge kind."""
    if kind == KIND_TAG:
        return Badge.objects.tags().visible_to(profile).ordered().with_customizations_for(profile).with_pin_counts()
    if kind == KIND_CATEGORY:
        return Badge.objects.categories().for_profile(profile).ordered().with_pin_counts()
    if kind == KIND_STATUS:
        return Badge.objects.statuses().for_profile(profile).ordered().with_pin_counts()
    if kind == KIND_USER:
        return Badge.objects.user_badges().visible_to(profile).ordered()
    msg = f"Unsupported badge kind: {kind}"
    raise ValueError(msg)


def _parent_candidates(profile: Profile, kind: str, exclude_id: int | None = None) -> QuerySet[Badge]:
    """Return badges eligible as parents for a badge of the given kind."""
    if kind == KIND_USER:
        qs = Badge.objects.user_badges().visible_to(profile)
    else:
        qs = Badge.objects.visible_to(profile)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs


def _rows_ctx(kind: str, profile: Profile, can_edit_global: bool = False, extra: dict | None = None) -> dict:
    """Build template context for organize_badge_rows.html and standalone index pages."""
    cfg = _config(kind)
    badge_list = _queryset_for_kind(kind, profile)
    ctx: dict = {
        **_BASE_CTX,
        "badges": badge_list,
        cfg.rows_context_key: badge_list,
        "kind": cfg.display_kind,
        "badge_url_kind": cfg.url_kind,
        "empty_icon": cfg.empty_icon,
        "empty_message": cfg.empty_message,
        "select_class": cfg.select_class,
        "select_data_name": cfg.select_data_name,
        "selectable": True,
        "editable": True,
        "deletable": True,
        "edit_target": cfg.edit_target,
        "rows_target": cfg.rows_target,
    }
    ctx["can_edit_global"] = can_edit_global
    if extra:
        ctx.update(extra)
    return ctx


def _render_rows(request: HttpRequest, kind: str, profile: Profile, extra: dict | None = None) -> HttpResponse:
    """Render the shared organize badge rows partial."""
    return render(
        request,
        "dashboard/partials/badges/organize_badge_rows.html",
        _rows_ctx(kind, profile, request.user.has_perm(_PERM), extra),
    )


def _merge_form_ctx(cfg: _KindConfig, badge: Badge, candidates: QuerySet[Badge]) -> dict:
    """Build template context for organize_badge_merge_form.html."""
    return {
        "badge": badge,
        "candidates": candidates,
        "kind": cfg.display_kind,
        "badge_url_kind": cfg.url_kind,
        "rows_target": cfg.rows_target,
        "singular_title": cfg.singular_title,
        "empty_icon": cfg.empty_icon,
        "show_location_count": cfg.show_location_count,
    }


def _can_modify_badge(request: HttpRequest, badge: Badge) -> bool:
    """Return True if the current user may edit or delete the badge."""
    if badge.kind == KIND_TAG and badge.profile is None:
        return request.user.has_perm(_PERM)
    if badge.profile is None:
        return False
    return badge.profile.user == request.user


def _owned_badge(request: HttpRequest, badge_id: int, kind: str, *, require_owner: bool = True) -> Badge | HttpResponseForbidden:
    """Load a badge of the expected kind and verify access."""
    badge = get_object_or_404(Badge, id=badge_id, kind=kind)
    if require_owner and not _can_modify_badge(request, badge):
        return HttpResponseForbidden()
    return badge


def _parse_ids_json(request: HttpRequest) -> tuple[list[int] | None, HttpResponse | None]:
    """Parse a JSON body containing an ``ids`` list."""
    try:
        data = json.loads(request.body)
        ids = [int(x) for x in data.get("ids", [])]
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, JsonResponse({"error": "Invalid data"}, status=400)
    if not ids:
        return None, HttpResponse("No items specified.", status=400)
    return ids, None


def _safe_int(value: object, default: int = 0) -> int:
    """Parse an integer from JSON or form data."""
    if isinstance(value, int):
        return value
    if isinstance(value, str | float | bytes | bytearray):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _parse_bulk_payload(data: dict) -> dict:
    """Extract optional bulk-edit fields from a JSON dict."""
    return {
        "has_icon": "icon" in data,
        "has_color": "color" in data,
        "has_description": "description" in data,
        "has_order": "order" in data,
        "icon": data.get("icon") or None,
        "color": data.get("color") or None,
        "description": data.get("description", ""),
        "order": _safe_int(data.get("order"), 0),
        "add_parent_ids": [int(x) for x in data.get("add_parent_ids", [])],
        "add_child_ids": [int(x) for x in data.get("add_child_ids", [])],
    }


def _apply_bulk_fields(badge: Badge, payload: dict) -> list[str]:
    """Apply bulk-edit field values to a badge; return updated field names."""
    update_fields: list[str] = []
    if payload["has_icon"]:
        badge.icon = payload["icon"]
        update_fields.append("icon")
    if payload["has_color"]:
        badge.color = payload["color"]
        update_fields.append("color")
    if payload["has_description"]:
        badge.description = payload["description"]
        update_fields.append("description")
    if payload["has_order"]:
        badge.order = payload["order"]
        update_fields.append("order")
    return update_fields


def _apply_custom_icon_from_post(badge: Badge, request: HttpRequest) -> None:
    """Update badge custom_icon from POST (upload or clear)."""
    custom_icon = request.FILES.get("custom_icon")
    if custom_icon:
        badge.custom_icon = _resize_custom_icon(custom_icon)
    elif request.POST.get("clear_custom_icon"):
        badge.custom_icon = None


def _apply_kind_conversion(badge: Badge, new_kind: str, profile: Profile) -> bool:
    """Apply a kind change to a badge. Returns True if kind changed."""
    if new_kind not in _ORGANIZE_KINDS or new_kind == badge.kind:
        return False
    badge.kind = new_kind
    if new_kind == KIND_STATUS:
        badge.profile = profile
    elif new_kind == KIND_TAG and badge.profile is None:
        pass
    elif new_kind == KIND_TAG:
        badge.profile = profile
    return True


class _BadgeKindMixin:
    """Mixin resolving ``kind`` from the ``badge_kind`` URL kwarg."""

    kind: str = ""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Resolve model kind from ``badge_kind`` before handling the request."""
        url_kind = kwargs.get("badge_kind")
        if url_kind:
            model_kind = _kind_from_url(str(url_kind))
            if model_kind is None:
                return HttpResponse(status=404)
            self.kind = model_kind
        elif not self.kind:
            return HttpResponse(status=404)
        return super().dispatch(request, *args, **kwargs)

    def _cfg(self) -> _KindConfig:
        return _config(self.kind)


class BadgeKindIndexView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Standalone index page for one badge kind (uses the shared Organize template)."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        """Render a single-kind badge management page.

        Args:
            request: The HTTP request.

        Returns:
            Rendered organize/index.html in standalone mode for this kind.
        """
        from urbanlens.dashboard.controllers.organize import build_organize_page_context

        cfg = self._cfg()
        ctx = build_organize_page_context(request, cfg.organize_tab)
        ctx.update(
            {
                "standalone_mode": True,
                "standalone_title": cfg.standalone_title,
                "standalone_subtitle": cfg.standalone_subtitle,
            },
        )
        return render(request, "dashboard/pages/organize/index.html", ctx)


class BadgeCreateView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Create a new badge of the configured kind (HTMX)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        profile = _request_profile(request)
        cfg = self._cfg()
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        parent_ids = request.POST.getlist("parent_ids")
        order = int(request.POST.get("order", 0))
        parent_order = Badge.initial_order_for_parents(profile, parent_ids)
        if parent_order is not None:
            order = parent_order

        custom_icon = request.FILES.get("custom_icon") or None
        if custom_icon:
            custom_icon = _resize_custom_icon(custom_icon)

        badge = Badge.objects.create(
            kind=self.kind,
            profile=profile,
            name=name,
            description=request.POST.get("description", "").strip() or None,
            icon=request.POST.get("icon") or None,
            color=request.POST.get("color") or None,
            custom_icon=custom_icon,
            order=order,
        )
        if parent_ids:
            valid_parents = _parent_candidates(profile, self.kind).filter(id__in=parent_ids).exclude(id=badge.id)
            badge.parents.set(valid_parents)

        child_ids = request.POST.getlist("child_ids")
        if child_ids:
            valid_children = _parent_candidates(profile, self.kind).filter(id__in=child_ids).exclude(id=badge.id)
            for child in valid_children:
                child.parents.add(badge)

        extra = {cfg.new_id_key: badge.id} if cfg.new_id_key else None
        if request.headers.get("Accept") == "application/json":
            return JsonResponse(
                {
                    "id": badge.id,
                    "name": badge.name,
                    "kind": badge.kind,
                    "icon": badge.icon or "",
                    "color": badge.color or "",
                }
            )
        return _render_rows(request, self.kind, profile, extra)


class BadgeEditView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Edit an existing badge (HTMX)."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        cfg = self._cfg()
        badge_id = _badge_id_from_kwargs(kwargs)
        badge = _owned_badge(request, badge_id, self.kind)
        if isinstance(badge, HttpResponseForbidden):
            return badge

        profile = _request_profile(request)
        selected_parents = badge.parents.all()
        selected_children = badge.children.all()
        selected_ids = {b.id for b in selected_parents} | {b.id for b in selected_children}
        available_parents = _parent_candidates(profile, self.kind, badge_id)

        return render(
            request,
            "dashboard/partials/badges/organize_badge_edit_form.html",
            {
                **_BASE_CTX,
                "badge": badge,
                "badge_url_kind": cfg.url_kind,
                "rows_target": cfg.rows_target,
                "singular_title": cfg.singular_title,
                "available_parents": available_parents,
                "selected_parents": selected_parents,
                "selected_children": selected_children,
                "selected_ids": selected_ids,
                "is_global": badge.kind == KIND_TAG and badge.profile is None,
                "show_kind_toggle": cfg.show_kind_toggle,
                "can_use_ai_features": user_has_feature(request.user, SiteFeature.AI),
            },
        )

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        badge_id = _badge_id_from_kwargs(kwargs)
        badge = _owned_badge(request, badge_id, self.kind)
        if isinstance(badge, HttpResponseForbidden):
            return badge

        profile = _request_profile(request)
        new_kind = request.POST.get("kind", self.kind)
        if new_kind not in _ORGANIZE_KINDS:
            new_kind = self.kind

        if new_kind != badge.kind and badge.is_protected:
            return HttpResponse("Protected statuses cannot be converted to another type.", status=403)

        if not badge.is_protected:
            name = request.POST.get("name", "").strip()
            if not name:
                return HttpResponse("Name is required.", status=400)
            badge.name = name

        badge.description = request.POST.get("description", "").strip() or None
        badge.icon = request.POST.get("icon") or None
        badge.color = request.POST.get("color") or None
        badge.order = int(request.POST.get("order", badge.order))

        # allow_auto_tag can only be changed when the user has AI features; and never
        # on the protected "Visited" badge.
        if not badge.is_protected:
            if user_has_feature(request.user, SiteFeature.AI):
                badge.allow_auto_tag = "allow_auto_tag" in request.POST
            badge.keywords = request.POST.get("keywords", "").strip() or None

        _apply_custom_icon_from_post(badge, request)

        kind_changed = _apply_kind_conversion(badge, new_kind, profile)
        badge.save()

        if kind_changed:
            badge.parents.clear()
        else:
            parent_ids = request.POST.getlist("parent_ids")
            valid_parents = _parent_candidates(profile, self.kind).filter(id__in=parent_ids).exclude(id=badge_id)
            badge.parents.set(valid_parents)

            child_ids = request.POST.getlist("child_ids")
            valid_children = _parent_candidates(profile, self.kind).filter(id__in=child_ids).exclude(id=badge_id)
            badge.children.set(valid_children)

        response = _render_rows(request, self.kind, profile)
        if kind_changed:
            response["X-Kind-Changed"] = new_kind
        return response


class BadgeDeleteView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Delete a badge (HTMX)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        badge_id = _badge_id_from_kwargs(kwargs)
        badge = _owned_badge(request, badge_id, self.kind)
        if isinstance(badge, HttpResponseForbidden):
            return badge
        if badge.is_protected:
            return HttpResponse(f"'{escape(badge.name)}' is a protected status and cannot be deleted.", status=403)

        badge.delete()
        return _render_rows(request, self.kind, _request_profile(request))


class BadgeRowsView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Return the rows partial for a badge kind."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        return _render_rows(request, self.kind, _request_profile(request))


class BadgeReorderView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Persist drag-and-drop order for tags, categories, or statuses."""

    def post(self, request: HttpRequest, *args, **kwargs) -> JsonResponse:
        if self.kind not in _ORGANIZE_KINDS:
            return JsonResponse({"error": "Not supported for this badge kind"}, status=404)
        try:
            data = json.loads(request.body)
            id_key = {
                KIND_TAG: "tag_ids",
                KIND_CATEGORY: "category_ids",
                KIND_STATUS: "status_ids",
            }[self.kind]
            badge_ids = [int(x) for x in data.get(id_key, [])]
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        profile = _request_profile(request)
        total = len(badge_ids)
        for i, badge_id in enumerate(badge_ids):
            Badge.objects.filter(id=badge_id, profile=profile, kind=self.kind).update(order=total - i)
        return JsonResponse({"ok": True})


class BadgeMergeView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Merge one user-owned badge into another (single-item merge form)."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        cfg = self._cfg()
        if not cfg.enable_single_merge:
            return HttpResponse(status=404)
        badge_id = _badge_id_from_kwargs(kwargs)
        profile = _request_profile(request)
        badge = get_object_or_404(_queryset_for_kind(self.kind, profile), id=badge_id)
        if badge.profile is None or badge.profile.user != request.user:
            return HttpResponseForbidden()
        if badge.is_protected:
            return HttpResponseForbidden()

        candidates = _queryset_for_kind(self.kind, profile).exclude(id=badge_id)
        return render(
            request,
            "dashboard/partials/badges/organize_badge_merge_form.html",
            _merge_form_ctx(cfg, badge, candidates),
        )

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        cfg = self._cfg()
        if not cfg.enable_single_merge:
            return HttpResponse(status=404)
        badge_id = _badge_id_from_kwargs(kwargs)
        profile = _request_profile(request)
        source = get_object_or_404(Badge, id=badge_id, kind=self.kind)
        if source.profile is None or source.profile.user != request.user:
            return HttpResponseForbidden()
        if source.is_protected:
            return HttpResponseForbidden()

        target_id = (request.POST.get("target_badge_id") or "").strip()
        if not target_id:
            return HttpResponse(f"Target {cfg.singular_title.lower()} is required.", status=400)

        target = get_object_or_404(_queryset_for_kind(self.kind, profile), id=target_id)
        if target.id == source.id:
            return HttpResponse(f"Cannot merge a {cfg.singular_title.lower()} into itself.", status=400)

        target.pins.add(*source.pins.all())
        target.wikis.add(*source.wikis.all())
        source.delete()

        return _render_rows(request, self.kind, profile)


class BadgeMultiMergeView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Merge multiple badges into a single target (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = json.loads(request.body)
            target_id = int(data.get("target_id", 0))
            source_ids = [int(x) for x in data.get("source_ids", [])]
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not target_id:
            return HttpResponse("target_id is required.", status=400)
        if not source_ids:
            return HttpResponse("At least one source_id is required.", status=400)

        profile = _request_profile(request)
        if self.kind == KIND_TAG:
            target = get_object_or_404(Badge.objects.tags().visible_to(profile), id=target_id)
            sources = Badge.objects.filter(id__in=source_ids, profile=profile, kind=KIND_TAG).exclude(id=target_id)
        elif self.kind == KIND_CATEGORY:
            target = get_object_or_404(Badge, id=target_id, kind=KIND_CATEGORY, profile=profile)
            sources = Badge.objects.filter(id__in=source_ids, kind=KIND_CATEGORY, profile=profile).exclude(id=target_id)
        elif self.kind == KIND_USER:
            target = get_object_or_404(Badge, id=target_id, kind=KIND_USER, profile=profile)
            sources = Badge.objects.filter(id__in=source_ids, kind=KIND_USER, profile=profile).exclude(id=target_id)
        else:
            target = get_object_or_404(Badge, id=target_id, kind=KIND_STATUS, profile=profile)
            sources = Badge.objects.filter(
                id__in=source_ids,
                kind=KIND_STATUS,
                profile=profile,
                is_protected=False,
            ).exclude(id=target_id)

        if not sources.exists():
            return HttpResponse(f"No valid source {self.kind}s.", status=400)

        if self.kind == KIND_USER:
            from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment

            for source in sources:
                for assignment in ProfileBadgeAssignment.objects.filter(badge=source):
                    ProfileBadgeAssignment.objects.get_or_create(
                        author=assignment.author,
                        subject=assignment.subject,
                        badge=target,
                    )
                source.delete()
        else:
            for source in sources:
                target.pins.add(*source.pins.all())
                if self.kind == KIND_CATEGORY:
                    target.wikis.add(*source.wikis.all())
                source.delete()

        return _render_rows(request, self.kind, profile)


class BadgeBulkDeleteView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Bulk-delete user-owned badges (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        ids, err = _parse_ids_json(request)
        if err:
            return err

        profile = _request_profile(request)
        qs = Badge.objects.filter(id__in=ids, profile=profile, kind=self.kind)
        if self.kind == KIND_STATUS:
            qs = qs.filter(is_protected=False)
        qs.delete()
        return _render_rows(request, self.kind, profile)


class BadgeBulkEditView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Bulk-edit icon, color, description, order, and parents (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        ids, err = _parse_ids_json(request)
        if err:
            return err

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid data"}, status=400)

        profile = _request_profile(request)
        payload = _parse_bulk_payload(data)
        badges = list(Badge.objects.filter(id__in=ids, profile=profile, kind=self.kind))
        if self.kind == KIND_STATUS:
            badges = [badge for badge in badges if not badge.is_protected]
        for badge in badges:
            update_fields = _apply_bulk_fields(badge, payload)
            if update_fields:
                badge.save(update_fields=update_fields)

        if payload["add_parent_ids"]:
            valid_parents = list(Badge.objects.visible_to(profile).filter(id__in=payload["add_parent_ids"]))
            for badge in badges:
                badge.parents.add(*[p for p in valid_parents if p.id != badge.id])

        if payload["add_child_ids"]:
            valid_children = list(Badge.objects.visible_to(profile).filter(id__in=payload["add_child_ids"]))
            for child in valid_children:
                child.parents.add(*[b for b in badges if b.id != child.id])

        return _render_rows(request, self.kind, profile)


class BadgeBulkConvertView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Convert badges to another kind (JSON POST).

    ``bulk-convert/`` swaps tag↔category. ``bulk-convert-status/`` sets ``target_kind`` to status.
    """

    target_kind: str = ""

    def _resolved_target_kind(self) -> str | None:
        """Return the destination kind for this convert request."""
        if self.target_kind:
            return self.target_kind
        if self.kind == KIND_TAG:
            return KIND_CATEGORY
        if self.kind == KIND_CATEGORY:
            return KIND_TAG
        if self.kind == KIND_STATUS:
            return None
        return None

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        new_kind = self._resolved_target_kind()
        if not new_kind:
            return HttpResponse(status=404)

        ids, err = _parse_ids_json(request)
        if err:
            return err

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid data"}, status=400)

        profile = _request_profile(request)
        payload = _parse_bulk_payload(data)
        badges = list(Badge.objects.filter(id__in=ids, profile=profile, kind=self.kind))
        if self.kind == KIND_STATUS:
            badges = [badge for badge in badges if not badge.is_protected]
        valid_parents = list(Badge.objects.visible_to(profile).filter(id__in=payload["add_parent_ids"])) if payload["add_parent_ids"] else []
        for badge in badges:
            _apply_bulk_fields(badge, payload)
            badge.kind = new_kind
            if new_kind == KIND_STATUS:
                badge.profile = profile
            badge.parents.clear()
            badge.save()
            if valid_parents:
                badge.parents.add(*[p for p in valid_parents if p.id != badge.id])

        if payload["add_child_ids"]:
            valid_children = list(Badge.objects.visible_to(profile).filter(id__in=payload["add_child_ids"]))
            for child in valid_children:
                child.parents.add(*[b for b in badges if b.id != child.id])

        return _render_rows(request, self.kind, profile)


class BadgeCustomizeView(_BadgeKindMixin, LoginRequiredMixin, View):
    """Per-user display overrides for global badges."""

    _CUSTOMIZE_FORM = "dashboard/partials/badges/organize_badge_customize_form.html"

    def _customize_ctx(self, badge: Badge, profile: Profile) -> dict:
        from urbanlens.dashboard.models.badges.customization import BadgeCustomization

        cfg = self._cfg()
        customization = BadgeCustomization.objects.filter(profile=profile, badge=badge).first()
        return {
            **_BASE_CTX,
            "badge": badge,
            "badge_url_kind": cfg.url_kind,
            "rows_target": cfg.rows_target,
            "customization": customization,
        }

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if self.kind == KIND_USER:
            return HttpResponse(status=404)
        badge_id = _badge_id_from_kwargs(kwargs)
        badge = get_object_or_404(Badge, id=badge_id, kind=self.kind)
        if badge.profile is not None:
            edit_view = BadgeEditView()
            edit_view.kind = self.kind
            return edit_view.get(request, badge_id=badge_id, badge_kind=kwargs.get("badge_kind"))
        return render(request, self._CUSTOMIZE_FORM, self._customize_ctx(badge, _request_profile(request)))

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if self.kind == KIND_USER:
            return HttpResponse(status=404)
        badge_id = _badge_id_from_kwargs(kwargs)
        badge = get_object_or_404(Badge, id=badge_id, kind=self.kind)
        if badge.profile is not None:
            edit_view = BadgeEditView()
            edit_view.kind = self.kind
            return edit_view.post(request, badge_id=badge_id, badge_kind=kwargs.get("badge_kind"))

        profile = _request_profile(request)
        from urbanlens.dashboard.models.badges.customization import BadgeCustomization

        if request.POST.get("action") == "clear":
            BadgeCustomization.objects.filter(profile=profile, badge=badge).delete()
        else:
            name = request.POST.get("name", "").strip() or None
            icon = request.POST.get("icon") or None
            color = request.POST.get("color") or None
            if name is None and icon is None and color is None:
                BadgeCustomization.objects.filter(profile=profile, badge=badge).delete()
            else:
                BadgeCustomization.objects.update_or_create(
                    profile=profile,
                    badge=badge,
                    defaults={"name": name, "icon": icon, "color": color},
                )

        return _render_rows(request, self.kind, profile)


def _all_badges(profile: Profile) -> QuerySet[Badge]:
    """Return all tag/category/status badges visible to the profile."""
    return Badge.objects.visible_to(profile).ordered()


def _pin_member_ids(pin: Pin) -> set[int]:
    """Return badge IDs assigned to a pin."""
    return set(pin.badges.values_list("id", flat=True))


def _wiki_member_ids(wiki) -> set[int]:
    """Return badge IDs assigned to a community wiki."""
    return set(wiki.badges.values_list("id", flat=True))


_MEMBERSHIP_PANEL = "dashboard/partials/badges/badge_membership_panel.html"
_MEMBERSHIP_URL_KIND = "category"  # URL prefix only; panel accepts all organize badge kinds.


def _membership_panel_ctx(
    profile: Profile,
    member_ids: set[int],
    *,
    panel_id: str,
    dialog_id_prefix: str,
    dialog_id_suffix: str,
    membership_route: str,
    obj_uuid: str,
    collapse_scope: str,
    empty_text: str | None = None,
) -> dict:
    """Build template context for badge_membership_panel.html."""
    ctx: dict = {
        "all_badges": _all_badges(profile),
        "member_ids": member_ids,
        "panel_id": panel_id,
        "dialog_id_prefix": dialog_id_prefix,
        "dialog_id_suffix": dialog_id_suffix,
        "membership_route": membership_route,
        "badge_url_kind": _MEMBERSHIP_URL_KIND,
        "obj_uuid": obj_uuid,
        "collapse_scope": collapse_scope,
    }
    if empty_text:
        ctx["empty_text"] = empty_text
    return ctx


def _membership_badge_id(request: HttpRequest) -> str | None:
    """Read a badge PK from membership add/remove POST data."""
    return request.POST.get("badge_id") or request.POST.get("category_id")


def _membership_kind_blocked(kwargs: dict[str, Any]) -> bool:
    """Return True when membership panels are not applicable to the URL badge kind."""
    url_kind = kwargs.get("badge_kind")
    return url_kind is not None and _kind_from_url(str(url_kind)) == KIND_USER


class BadgePinMembershipView(LoginRequiredMixin, View):
    """Add or remove any organize badge on a pin (HTMX panel on pin detail)."""

    def get(self, request: HttpRequest, pin_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _request_profile(request)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            _membership_panel_ctx(
                profile,
                _pin_member_ids(pin),
                panel_id="category-panel",
                dialog_id_prefix="category-add-dialog-",
                dialog_id_suffix=pin_slug,
                membership_route="pin",
                obj_uuid=pin_slug,
                collapse_scope="pin",
            ),
        )

    def post(self, request: HttpRequest, pin_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _request_profile(request)
        badge_id = _membership_badge_id(request)
        action = request.POST.get("action")
        badge = get_object_or_404(Badge, id=badge_id, kind__in=_ORGANIZE_KINDS)
        if action == "add":
            pin.badges.add(badge)
        elif action == "remove":
            pin.badges.remove(badge)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            _membership_panel_ctx(
                profile,
                _pin_member_ids(pin),
                panel_id="category-panel",
                dialog_id_prefix="category-add-dialog-",
                dialog_id_suffix=pin_slug,
                membership_route="pin",
                obj_uuid=pin_slug,
                collapse_scope="pin",
            ),
        )


class BadgeLocationMembershipView(LoginRequiredMixin, View):
    """Add or remove badges on a community wiki (HTMX panel on wiki page)."""

    def _resolve_wiki(self, location_slug: str):
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = get_object_or_404(Location, slug=location_slug)
        return get_object_or_404(Wiki, location=location)

    def get(self, request: HttpRequest, location_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        wiki = self._resolve_wiki(location_slug)
        profile = _request_profile(request)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            _membership_panel_ctx(
                profile,
                _wiki_member_ids(wiki),
                panel_id="category-location-panel",
                dialog_id_prefix="category-loc-dialog-",
                dialog_id_suffix=location_slug,
                membership_route="location",
                obj_uuid=location_slug,
                collapse_scope="wiki",
                empty_text="No badges. Click + to add one.",
            ),
        )

    def post(self, request: HttpRequest, location_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        wiki = self._resolve_wiki(location_slug)
        profile = _request_profile(request)
        badge_id = _membership_badge_id(request)
        action = request.POST.get("action")
        badge = get_object_or_404(Badge, id=badge_id, kind__in=_ORGANIZE_KINDS)
        if action == "add":
            wiki.badges.add(badge)
        elif action == "remove":
            wiki.badges.remove(badge)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            _membership_panel_ctx(
                profile,
                _wiki_member_ids(wiki),
                panel_id="category-location-panel",
                dialog_id_prefix="category-loc-dialog-",
                dialog_id_suffix=location_slug,
                membership_route="location",
                obj_uuid=location_slug,
                collapse_scope="wiki",
                empty_text="No badges. Click + to add one.",
            ),
        )
