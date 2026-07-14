"""Unified label controller for tag, category, status, and people label CRUD.

All organize label kinds are ``Label`` rows distinguished by ``kind``.
Views read ``label_kind`` from the URL (see ``urls.py``).
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
from django.utils import timezone
from django.utils.html import escape
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import (
    COLOR_CHOICES,
    ICON_CATEGORIES,
    ICON_CHOICES,
    KIND_CATEGORY,
    KIND_MEDIA,
    KIND_STATUS,
    KIND_TAG,
    KIND_USER,
    Label,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

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


_PERM = "dashboard.edit_global_label"
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
    "media": KIND_MEDIA,
}
MODEL_KIND_TO_URL: dict[str, str] = {
    KIND_TAG: "tag",
    KIND_CATEGORY: "category",
    KIND_STATUS: "status",
    KIND_USER: "people",
    KIND_MEDIA: "media",
}

_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


@dataclass(frozen=True)
class _KindConfig:
    """Per-kind template and URL metadata for organize label views."""

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
    edit_target: str = "#label-edit-dialog-body"
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
        rows_context_key="user_labels",
        rows_target="#people-label-rows",
        select_class="people-sel-cb",
        select_data_name="people",
        empty_icon="person",
        empty_message="No people labels yet. Create one to start organizing people.",
        organize_tab="people",
        standalone_title="People Labels",
        standalone_subtitle="Private labels for organizing people in your network.",
        show_kind_toggle=False,
        edit_target="#people-label-edit-dialog-body",
        enable_single_merge=False,
    ),
    KIND_MEDIA: _KindConfig(
        kind=KIND_MEDIA,
        url_kind="media",
        display_kind="media",
        singular_title="Media Label",
        rows_context_key="media_labels",
        rows_target="#media-label-rows",
        select_class="media-sel-cb",
        select_data_name="media",
        empty_icon="perm_media",
        empty_message="No media labels yet. Create one to help you find your photos, videos, and documents in search.",
        organize_tab="media",
        standalone_title="Media Labels",
        standalone_subtitle="Labels to help you find your photos, videos, and documents in site search.",
        show_kind_toggle=False,
        edit_target="#media-label-edit-dialog-body",
        enable_single_merge=False,
    ),
}


def _config(kind: str) -> _KindConfig:
    """Return configuration for an organize label kind.

    Args:
        kind: Label kind string (tag, category, status, or user).

    Returns:
        Frozen config for the kind.

    Raises:
        KeyError: If kind is not a supported organize label kind.
    """
    return _KIND_CONFIG[kind]


def _kind_from_url(url_kind: str) -> str | None:
    """Map a URL ``label_kind`` segment to a model kind constant."""
    return URL_KIND_TO_MODEL.get(url_kind)


def _label_id_from_kwargs(kwargs: dict[str, Any]) -> int:
    """Extract a label PK from URL kwargs.

    Args:
        kwargs: URL keyword arguments from the view.

    Returns:
        Integer label primary key.

    Raises:
        KeyError: If no label id is present.
    """
    if "label_id" in kwargs:
        return int(kwargs["label_id"])
    for key in ("tag_id", "cat_id", "status_id"):
        if key in kwargs:
            return int(kwargs[key])
    msg = "No label id in URL kwargs"
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


def _queryset_for_kind(kind: str, profile: Profile) -> QuerySet[Label]:
    """Return the display queryset for a label kind."""
    if kind == KIND_TAG:
        return Label.objects.tags().visible_to(profile).ordered().with_customizations_for(profile).with_pin_counts()
    if kind == KIND_CATEGORY:
        return Label.objects.categories().for_profile(profile).ordered().with_pin_counts()
    if kind == KIND_STATUS:
        return Label.objects.statuses().for_profile(profile).ordered().with_pin_counts()
    if kind == KIND_USER:
        return Label.objects.user_labels().visible_to(profile).ordered()
    if kind == KIND_MEDIA:
        return Label.objects.media().visible_to(profile).ordered()
    msg = f"Unsupported label kind: {kind}"
    raise ValueError(msg)


def _parent_candidates(profile: Profile, kind: str, exclude_id: int | None = None) -> QuerySet[Label]:
    """Return labels eligible as parents for a label of the given kind."""
    if kind == KIND_USER:
        qs = Label.objects.user_labels().visible_to(profile)
    elif kind == KIND_MEDIA:
        qs = Label.objects.media().visible_to(profile)
    else:
        qs = Label.objects.visible_to(profile)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs


def _rows_ctx(kind: str, profile: Profile, can_edit_global: bool = False, extra: dict | None = None) -> dict:
    """Build template context for organize_label_rows.html and standalone index pages."""
    cfg = _config(kind)
    label_list = _queryset_for_kind(kind, profile)
    ctx: dict = {
        **_BASE_CTX,
        "labels": label_list,
        cfg.rows_context_key: label_list,
        "kind": cfg.display_kind,
        "label_url_kind": cfg.url_kind,
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
    """Render the shared organize label rows partial."""
    return render(
        request,
        "dashboard/partials/labels/organize_label_rows.html",
        _rows_ctx(kind, profile, request.user.has_perm(_PERM), extra),
    )


def _merge_form_ctx(cfg: _KindConfig, label: Label, candidates: QuerySet[Label]) -> dict:
    """Build template context for organize_label_merge_form.html."""
    return {
        "label": label,
        "candidates": candidates,
        "kind": cfg.display_kind,
        "label_url_kind": cfg.url_kind,
        "rows_target": cfg.rows_target,
        "singular_title": cfg.singular_title,
        "empty_icon": cfg.empty_icon,
        "show_location_count": cfg.show_location_count,
    }


def _can_modify_label(request: HttpRequest, label: Label) -> bool:
    """Return True if the current user may edit or delete the label."""
    if label.kind == KIND_TAG and label.profile is None:
        return request.user.has_perm(_PERM)
    if label.profile is None:
        return False
    return label.profile.user == request.user


def _owned_label(request: HttpRequest, label_id: int, kind: str, *, require_owner: bool = True) -> Label | HttpResponseForbidden:
    """Load a label of the expected kind and verify access."""
    label = get_object_or_404(Label, id=label_id, kind=kind)
    if require_owner and not _can_modify_label(request, label):
        return HttpResponseForbidden()
    return label


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


def _apply_bulk_fields(label: Label, payload: dict) -> list[str]:
    """Apply bulk-edit field values to a label; return updated field names."""
    update_fields: list[str] = []
    if payload["has_icon"]:
        label.icon = payload["icon"]
        update_fields.append("icon")
    if payload["has_color"]:
        label.color = payload["color"]
        update_fields.append("color")
    if payload["has_description"]:
        label.description = payload["description"]
        update_fields.append("description")
    if payload["has_order"]:
        label.order = payload["order"]
        update_fields.append("order")
    return update_fields


def _uploaded_custom_icon(request: HttpRequest) -> UploadedFile | None:
    """Return the submitted custom-icon file, if any.

    ``_icon_picker.html`` names its file input ``custom_icon-<picker_id>`` (scoped
    per widget instance) rather than a bare ``custom_icon``, so that two icon
    pickers rendered on the same page can never collide on field name even if a
    future change nests them in the same form. Each submitted form only ever
    contains one such field, so the first match is unambiguous.
    """
    for field_name in request.FILES:
        if field_name == "custom_icon" or field_name.startswith("custom_icon-"):
            return request.FILES.get(field_name)
    return None


def _apply_custom_icon_from_post(label: Label, request: HttpRequest) -> None:
    """Update label custom_icon from POST (upload or clear)."""
    custom_icon = _uploaded_custom_icon(request)
    if custom_icon:
        label.custom_icon = _resize_custom_icon(custom_icon)
    elif request.POST.get("clear_custom_icon"):
        label.custom_icon = None


def _apply_kind_conversion(label: Label, new_kind: str, profile: Profile) -> bool:
    """Apply a kind change to a label. Returns True if kind changed."""
    if new_kind not in _ORGANIZE_KINDS or new_kind == label.kind:
        return False
    label.kind = new_kind
    if new_kind == KIND_STATUS:
        label.profile = profile
    elif new_kind == KIND_TAG and label.profile is None:
        pass
    elif new_kind == KIND_TAG:
        label.profile = profile
    return True


class _LabelKindMixin:
    """Mixin resolving ``kind`` from the ``label_kind`` URL kwarg."""

    kind: str = ""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Resolve model kind from ``label_kind`` before handling the request."""
        url_kind = kwargs.get("label_kind")
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


class LabelKindIndexView(_LabelKindMixin, LoginRequiredMixin, View):
    """Standalone index page for one label kind (uses the shared Organize template)."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        """Render a single-kind label management page.

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


class LabelCreateView(_LabelKindMixin, LoginRequiredMixin, View):
    """Create a new label of the configured kind (HTMX)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        profile = _request_profile(request)
        cfg = self._cfg()
        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)

        parent_ids = request.POST.getlist("parent_ids")
        order = int(request.POST.get("order", 0))
        parent_order = Label.initial_order_for_parents(profile, parent_ids)
        if parent_order is not None:
            order = parent_order

        custom_icon = _uploaded_custom_icon(request)
        if custom_icon:
            custom_icon = _resize_custom_icon(custom_icon)

        label = Label.objects.create(
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
            valid_parents = _parent_candidates(profile, self.kind).filter(id__in=parent_ids).exclude(id=label.id)
            label.parents.set(valid_parents)

        child_ids = request.POST.getlist("child_ids")
        if child_ids:
            valid_children = _parent_candidates(profile, self.kind).filter(id__in=child_ids).exclude(id=label.id)
            for child in valid_children:
                child.parents.add(label)

        extra = {cfg.new_id_key: label.id} if cfg.new_id_key else None
        if request.headers.get("Accept") == "application/json":
            return JsonResponse(
                {
                    "id": label.id,
                    "name": label.name,
                    "kind": label.kind,
                    "icon": label.icon or "",
                    "color": label.color or "",
                }
            )
        return _render_rows(request, self.kind, profile, extra)


class LabelEditView(_LabelKindMixin, LoginRequiredMixin, View):
    """Edit an existing label (HTMX)."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        cfg = self._cfg()
        label_id = _label_id_from_kwargs(kwargs)
        label = _owned_label(request, label_id, self.kind)
        if isinstance(label, HttpResponseForbidden):
            return label

        profile = _request_profile(request)
        selected_parents = label.parents.all()
        selected_children = label.children.all()
        selected_ids = {b.id for b in selected_parents} | {b.id for b in selected_children}
        available_parents = _parent_candidates(profile, self.kind, label_id)

        return render(
            request,
            "dashboard/partials/labels/organize_label_edit_form.html",
            {
                **_BASE_CTX,
                "label": label,
                "label_url_kind": cfg.url_kind,
                "rows_target": cfg.rows_target,
                "singular_title": cfg.singular_title,
                "available_parents": available_parents,
                "selected_parents": selected_parents,
                "selected_children": selected_children,
                "selected_ids": selected_ids,
                "is_global": label.kind == KIND_TAG and label.profile is None,
                "show_kind_toggle": cfg.show_kind_toggle,
                "can_use_ai_features": user_has_feature(request.user, SiteFeature.AI),
            },
        )

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        label_id = _label_id_from_kwargs(kwargs)
        label = _owned_label(request, label_id, self.kind)
        if isinstance(label, HttpResponseForbidden):
            return label

        profile = _request_profile(request)
        new_kind = request.POST.get("kind", self.kind)
        # Kind conversion is only ever valid tag<->category<->status; a label
        # whose OWN kind isn't one of those (people, media) must never be
        # convertible via a crafted `kind` POST value, even though `new_kind`
        # alone might look like a valid organize kind.
        if new_kind not in _ORGANIZE_KINDS or self.kind not in _ORGANIZE_KINDS:
            new_kind = self.kind

        if new_kind != label.kind and label.is_protected:
            return HttpResponse("Protected statuses cannot be converted to another type.", status=403)

        if not label.is_protected:
            name = request.POST.get("name", "").strip()
            if not name:
                return HttpResponse("Name is required.", status=400)
            label.name = name

        label.description = request.POST.get("description", "").strip() or None
        label.icon = request.POST.get("icon") or None
        label.color = request.POST.get("color") or None
        label.order = int(request.POST.get("order", label.order))

        # allow_auto_tag can only be changed when the user has AI features; and never
        # on the protected "Visited" label.
        if not label.is_protected:
            if user_has_feature(request.user, SiteFeature.AI):
                label.allow_auto_tag = "allow_auto_tag" in request.POST
            label.keywords = request.POST.get("keywords", "").strip() or None

        _apply_custom_icon_from_post(label, request)

        kind_changed = _apply_kind_conversion(label, new_kind, profile)
        label.save()

        # A label's icon/color/name feed into every pin's cached map marker
        # (Pin.effective_icon, Pin.effective_color, the "statuses" list in
        # to_detail_json()) without touching the Pin row itself, so the
        # client's cache-freshness check (keyed to Max(Pin.updated)) would
        # otherwise never notice this change and keep serving stale markers.
        Pin.objects.filter(profile=profile, labels=label).update(updated=timezone.now())

        if kind_changed:
            label.parents.clear()
        else:
            parent_ids = request.POST.getlist("parent_ids")
            valid_parents = _parent_candidates(profile, self.kind).filter(id__in=parent_ids).exclude(id=label_id)
            label.parents.set(valid_parents)

            child_ids = request.POST.getlist("child_ids")
            valid_children = _parent_candidates(profile, self.kind).filter(id__in=child_ids).exclude(id=label_id)
            label.children.set(valid_children)

        response = _render_rows(request, self.kind, profile)
        if kind_changed:
            response["X-Kind-Changed"] = new_kind
        return response


class LabelDeleteView(_LabelKindMixin, LoginRequiredMixin, View):
    """Delete a label (HTMX)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        label_id = _label_id_from_kwargs(kwargs)
        label = _owned_label(request, label_id, self.kind)
        if isinstance(label, HttpResponseForbidden):
            return label
        if label.is_protected:
            return HttpResponse(f"'{escape(label.name)}' is a protected status and cannot be deleted.", status=403)

        label.delete()
        return _render_rows(request, self.kind, _request_profile(request))


class LabelRowsView(_LabelKindMixin, LoginRequiredMixin, View):
    """Return the rows partial for a label kind."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        return _render_rows(request, self.kind, _request_profile(request))


class LabelReorderView(_LabelKindMixin, LoginRequiredMixin, View):
    """Persist drag-and-drop order for tags, categories, or statuses."""

    def post(self, request: HttpRequest, *args, **kwargs) -> JsonResponse:
        if self.kind not in _ORGANIZE_KINDS:
            return JsonResponse({"error": "Not supported for this label kind"}, status=404)
        try:
            data = json.loads(request.body)
            id_key = {
                KIND_TAG: "tag_ids",
                KIND_CATEGORY: "category_ids",
                KIND_STATUS: "status_ids",
            }[self.kind]
            label_ids = [int(x) for x in data.get(id_key, [])]
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        profile = _request_profile(request)
        total = len(label_ids)
        for i, label_id in enumerate(label_ids):
            Label.objects.filter(id=label_id, profile=profile, kind=self.kind).update(order=total - i)
        return JsonResponse({"ok": True})


class LabelMergeView(_LabelKindMixin, LoginRequiredMixin, View):
    """Merge one user-owned label into another (single-item merge form)."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        cfg = self._cfg()
        if not cfg.enable_single_merge:
            return HttpResponse(status=404)
        label_id = _label_id_from_kwargs(kwargs)
        profile = _request_profile(request)
        label = get_object_or_404(_queryset_for_kind(self.kind, profile), id=label_id)
        if label.profile is None or label.profile.user != request.user:
            return HttpResponseForbidden()
        if label.is_protected:
            return HttpResponseForbidden()

        candidates = _queryset_for_kind(self.kind, profile).exclude(id=label_id)
        return render(
            request,
            "dashboard/partials/labels/organize_label_merge_form.html",
            _merge_form_ctx(cfg, label, candidates),
        )

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        cfg = self._cfg()
        if not cfg.enable_single_merge:
            return HttpResponse(status=404)
        label_id = _label_id_from_kwargs(kwargs)
        profile = _request_profile(request)
        source = get_object_or_404(Label, id=label_id, kind=self.kind)
        if source.profile is None or source.profile.user != request.user:
            return HttpResponseForbidden()
        if source.is_protected:
            return HttpResponseForbidden()

        target_id = (request.POST.get("target_label_id") or "").strip()
        if not target_id:
            return HttpResponse(f"Target {cfg.singular_title.lower()} is required.", status=400)

        target = get_object_or_404(_queryset_for_kind(self.kind, profile), id=target_id)
        if target.id == source.id:
            return HttpResponse(f"Cannot merge a {cfg.singular_title.lower()} into itself.", status=400)

        target.pins.add(*source.pins.all())
        target.wikis.add(*source.wikis.all())
        source.delete()

        return _render_rows(request, self.kind, profile)


class LabelMultiMergeView(_LabelKindMixin, LoginRequiredMixin, View):
    """Merge multiple labels into a single target (JSON POST)."""

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
            target = get_object_or_404(Label.objects.tags().visible_to(profile), id=target_id)
            sources = Label.objects.filter(id__in=source_ids, profile=profile, kind=KIND_TAG).exclude(id=target_id)
        elif self.kind == KIND_CATEGORY:
            target = get_object_or_404(Label, id=target_id, kind=KIND_CATEGORY, profile=profile)
            sources = Label.objects.filter(id__in=source_ids, kind=KIND_CATEGORY, profile=profile).exclude(id=target_id)
        elif self.kind == KIND_USER:
            target = get_object_or_404(Label, id=target_id, kind=KIND_USER, profile=profile)
            sources = Label.objects.filter(id__in=source_ids, kind=KIND_USER, profile=profile).exclude(id=target_id)
        elif self.kind == KIND_MEDIA:
            target = get_object_or_404(Label, id=target_id, kind=KIND_MEDIA, profile=profile)
            sources = Label.objects.filter(id__in=source_ids, kind=KIND_MEDIA, profile=profile).exclude(id=target_id)
        else:
            target = get_object_or_404(Label, id=target_id, kind=KIND_STATUS, profile=profile)
            sources = Label.objects.filter(
                id__in=source_ids,
                kind=KIND_STATUS,
                profile=profile,
                is_protected=False,
            ).exclude(id=target_id)

        if not sources.exists():
            return HttpResponse(f"No valid source {self.kind}s.", status=400)

        if self.kind == KIND_USER:
            from urbanlens.dashboard.models.labels.profile_assignment import ProfileLabelAssignment

            for source in sources:
                for assignment in ProfileLabelAssignment.objects.filter(label=source):
                    ProfileLabelAssignment.objects.get_or_create(
                        author=assignment.author,
                        subject=assignment.subject,
                        label=target,
                    )
                source.delete()
        elif self.kind == KIND_MEDIA:
            for source in sources:
                target.images.add(*source.images.all())
                source.delete()
        else:
            for source in sources:
                target.pins.add(*source.pins.all())
                if self.kind == KIND_CATEGORY:
                    target.wikis.add(*source.wikis.all())
                source.delete()

        return _render_rows(request, self.kind, profile)


class LabelBulkDeleteView(_LabelKindMixin, LoginRequiredMixin, View):
    """Bulk-delete user-owned labels (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        ids, err = _parse_ids_json(request)
        if err:
            return err

        profile = _request_profile(request)
        qs = Label.objects.filter(id__in=ids, profile=profile, kind=self.kind)
        if self.kind == KIND_STATUS:
            qs = qs.filter(is_protected=False)
        qs.delete()
        return _render_rows(request, self.kind, profile)


class LabelBulkEditView(_LabelKindMixin, LoginRequiredMixin, View):
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
        labels = list(Label.objects.filter(id__in=ids, profile=profile, kind=self.kind))
        if self.kind == KIND_STATUS:
            labels = [label for label in labels if not label.is_protected]
        for label in labels:
            update_fields = _apply_bulk_fields(label, payload)
            if update_fields:
                label.save(update_fields=update_fields)

        if payload["add_parent_ids"]:
            valid_parents = list(Label.objects.visible_to(profile).filter(id__in=payload["add_parent_ids"]))
            for label in labels:
                label.parents.add(*[p for p in valid_parents if p.id != label.id])

        if payload["add_child_ids"]:
            valid_children = list(Label.objects.visible_to(profile).filter(id__in=payload["add_child_ids"]))
            for child in valid_children:
                child.parents.add(*[b for b in labels if b.id != child.id])

        return _render_rows(request, self.kind, profile)


class LabelBulkConvertView(_LabelKindMixin, LoginRequiredMixin, View):
    """Convert labels to another kind (JSON POST).

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
        labels = list(Label.objects.filter(id__in=ids, profile=profile, kind=self.kind))
        if self.kind == KIND_STATUS:
            labels = [label for label in labels if not label.is_protected]
        valid_parents = list(Label.objects.visible_to(profile).filter(id__in=payload["add_parent_ids"])) if payload["add_parent_ids"] else []
        for label in labels:
            _apply_bulk_fields(label, payload)
            label.kind = new_kind
            if new_kind == KIND_STATUS:
                label.profile = profile
            label.parents.clear()
            label.save()
            if valid_parents:
                label.parents.add(*[p for p in valid_parents if p.id != label.id])

        if payload["add_child_ids"]:
            valid_children = list(Label.objects.visible_to(profile).filter(id__in=payload["add_child_ids"]))
            for child in valid_children:
                child.parents.add(*[b for b in labels if b.id != child.id])

        return _render_rows(request, self.kind, profile)


class LabelCustomizeView(_LabelKindMixin, LoginRequiredMixin, View):
    """Per-user display overrides for global labels."""

    _CUSTOMIZE_FORM = "dashboard/partials/labels/organize_label_customize_form.html"

    def _customize_ctx(self, label: Label, profile: Profile) -> dict:
        from urbanlens.dashboard.models.labels.customization import LabelCustomization

        cfg = self._cfg()
        customization = LabelCustomization.objects.filter(profile=profile, label=label).first()
        return {
            **_BASE_CTX,
            "label": label,
            "label_url_kind": cfg.url_kind,
            "rows_target": cfg.rows_target,
            "customization": customization,
        }

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if self.kind == KIND_USER:
            return HttpResponse(status=404)
        label_id = _label_id_from_kwargs(kwargs)
        label = get_object_or_404(Label, id=label_id, kind=self.kind)
        if label.profile is not None:
            edit_view = LabelEditView()
            edit_view.kind = self.kind
            return edit_view.get(request, label_id=label_id, label_kind=kwargs.get("label_kind"))
        return render(request, self._CUSTOMIZE_FORM, self._customize_ctx(label, _request_profile(request)))

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if self.kind == KIND_USER:
            return HttpResponse(status=404)
        label_id = _label_id_from_kwargs(kwargs)
        label = get_object_or_404(Label, id=label_id, kind=self.kind)
        if label.profile is not None:
            edit_view = LabelEditView()
            edit_view.kind = self.kind
            return edit_view.post(request, label_id=label_id, label_kind=kwargs.get("label_kind"))

        profile = _request_profile(request)
        from urbanlens.dashboard.models.labels.customization import LabelCustomization

        if request.POST.get("action") == "clear":
            LabelCustomization.objects.filter(profile=profile, label=label).delete()
        else:
            name = request.POST.get("name", "").strip() or None
            icon = request.POST.get("icon") or None
            color = request.POST.get("color") or None
            if name is None and icon is None and color is None:
                LabelCustomization.objects.filter(profile=profile, label=label).delete()
            else:
                LabelCustomization.objects.update_or_create(
                    profile=profile,
                    label=label,
                    defaults={"name": name, "icon": icon, "color": color},
                )

        # See the matching comment in LabelEditView.post - a customization
        # changes how this profile's pins render on the map without touching
        # any Pin row, so the cache-freshness check needs a manual nudge.
        Pin.objects.filter(profile=profile, labels=label).update(updated=timezone.now())

        return _render_rows(request, self.kind, profile)


def _all_labels(profile: Profile) -> QuerySet[Label]:
    """Return all tag/category/status labels visible to the profile."""
    return Label.objects.visible_to(profile).location_labels().ordered()


def _pin_member_ids(pin: Pin) -> set[int]:
    """Return label IDs assigned to a pin."""
    return set(pin.labels.values_list("id", flat=True))


def _wiki_member_ids(wiki) -> set[int]:
    """Return label IDs assigned to a community wiki."""
    return set(wiki.labels.values_list("id", flat=True))


def _image_member_ids(image: Image) -> set[int]:
    """Return media label IDs assigned to a photo/video/document."""
    return set(image.labels.values_list("id", flat=True))


_MEMBERSHIP_PANEL = "dashboard/partials/labels/label_membership_panel.html"
_MEMBERSHIP_URL_KIND = "category"  # URL prefix only; panel accepts all organize label kinds.


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
    embedded: bool = False,
    labels_override: QuerySet[Label] | None = None,
) -> dict:
    """Build template context for label_membership_panel.html."""
    ctx: dict = {
        "all_labels": labels_override if labels_override is not None else _all_labels(profile),
        "member_ids": member_ids,
        "panel_id": panel_id,
        "dialog_id_prefix": dialog_id_prefix,
        "dialog_id_suffix": dialog_id_suffix,
        "membership_route": membership_route,
        "label_url_kind": _MEMBERSHIP_URL_KIND,
        "obj_uuid": obj_uuid,
        "collapse_scope": collapse_scope,
        "embedded": embedded,
    }
    if empty_text:
        ctx["empty_text"] = empty_text
    return ctx


def _membership_label_id(request: HttpRequest) -> str | None:
    """Read a label PK from membership add/remove POST data."""
    return request.POST.get("label_id") or request.POST.get("category_id")


def _membership_kind_blocked(kwargs: dict[str, Any]) -> bool:
    """Return True when membership panels are not applicable to the URL label kind."""
    url_kind = kwargs.get("label_kind")
    return url_kind is not None and _kind_from_url(str(url_kind)) == KIND_USER


class LabelPinMembershipView(LoginRequiredMixin, View):
    """Add or remove any organize label on a pin (HTMX panel on pin detail)."""

    @staticmethod
    def _ctx(profile: Profile, pin: Pin, pin_slug: str) -> dict:
        ctx = _membership_panel_ctx(
            profile,
            _pin_member_ids(pin),
            panel_id="category-panel",
            dialog_id_prefix="category-add-dialog-",
            dialog_id_suffix=pin_slug,
            membership_route="pin",
            obj_uuid=pin_slug,
            collapse_scope="pin",
            embedded=True,
        )
        # The pin's Organize dialog combines label-picking with list-picking under
        # tabs (see _label_dialog.html), so this panel also needs the profile's lists.
        ctx["dialog_title"] = "Add to Pin"
        ctx["pin_lists"] = list(PinList.objects.filter(profile=profile).order_by("name"))
        return ctx

    def get(self, request: HttpRequest, pin_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _request_profile(request)
        return render(request, _MEMBERSHIP_PANEL, self._ctx(profile, pin, pin_slug))

    def post(self, request: HttpRequest, pin_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _request_profile(request)
        label_id = _membership_label_id(request)
        action = request.POST.get("action")
        label = get_object_or_404(Label.objects.visible_to(profile), id=label_id, kind__in=_ORGANIZE_KINDS)
        if action == "add":
            pin.labels.add(label)
        elif action == "remove":
            pin.labels.remove(label)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            self._ctx(profile, pin, pin_slug),
        )


class LabelLocationMembershipView(LoginRequiredMixin, View):
    """Add or remove labels on a community wiki (HTMX panel on wiki page)."""

    def get(self, request: HttpRequest, location_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
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
                empty_text="No labels. Click + to add one.",
            ),
        )

    def post(self, request: HttpRequest, location_slug: str, *args, **kwargs) -> HttpResponse:
        if _membership_kind_blocked(kwargs):
            return HttpResponse(status=404)
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        label_id = _membership_label_id(request)
        action = request.POST.get("action")
        label = get_object_or_404(Label.objects.visible_to(profile), id=label_id, kind__in=_ORGANIZE_KINDS)
        if action == "add":
            wiki.labels.add(label)
        elif action == "remove":
            wiki.labels.remove(label)
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
                empty_text="No labels. Click + to add one.",
            ),
        )


class LabelImageMembershipView(LoginRequiredMixin, View):
    """Add or remove media labels on a photo/video/document (HTMX panel).

    Unlike pin/location membership, this is scoped to the owner's own media
    labels (kind='media') only - media labels help find the item in search,
    they never apply to pins or wikis.
    """

    def _get_owned_image(self, request: HttpRequest, image_uuid: str) -> Image:
        return get_object_or_404(Image, uuid=image_uuid, profile__user=request.user)

    def get(self, request: HttpRequest, image_uuid: str, *args, **kwargs) -> HttpResponse:
        image = self._get_owned_image(request, image_uuid)
        profile = _request_profile(request)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            _membership_panel_ctx(
                profile,
                _image_member_ids(image),
                panel_id="media-label-panel",
                dialog_id_prefix="media-label-dialog-",
                dialog_id_suffix=image_uuid,
                membership_route="image",
                obj_uuid=image_uuid,
                collapse_scope="image",
                empty_text="No media labels. Click + to add one.",
                labels_override=Label.objects.visible_to(profile).media().ordered(),
            ),
        )

    def post(self, request: HttpRequest, image_uuid: str, *args, **kwargs) -> HttpResponse:
        image = self._get_owned_image(request, image_uuid)
        profile = _request_profile(request)
        label_id = _membership_label_id(request)
        action = request.POST.get("action")
        label = get_object_or_404(Label.objects.visible_to(profile), id=label_id, kind=KIND_MEDIA)
        if action == "add":
            image.labels.add(label)
        elif action == "remove":
            image.labels.remove(label)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            _membership_panel_ctx(
                profile,
                _image_member_ids(image),
                panel_id="media-label-panel",
                dialog_id_prefix="media-label-dialog-",
                dialog_id_suffix=image_uuid,
                membership_route="image",
                obj_uuid=image_uuid,
                collapse_scope="image",
                empty_text="No media labels. Click + to add one.",
                labels_override=Label.objects.visible_to(profile).media().ordered(),
            ),
        )
