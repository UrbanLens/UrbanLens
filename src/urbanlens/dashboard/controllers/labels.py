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
from PIL.Image import DecompressionBombError as PILDecompressionBombError

from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval, WikiAutoRemoval
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import DEFAULT_LABEL_COLOR
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
from urbanlens.dashboard.models.pin.signals import refresh_map_pin_cache_for_label_ids
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature
from urbanlens.dashboard.services.core.colors import clean_color
from urbanlens.dashboard.services.core.icons import clean_icon
from urbanlens.dashboard.services.core.numbers import safe_int
from urbanlens.dashboard.services.core.text_limits import column_length_error, column_max_length
from urbanlens.dashboard.services.labels.customization import clear_label_customization, upsert_label_customization
from urbanlens.dashboard.services.labels.hierarchy import would_create_cycle
from urbanlens.dashboard.services.labels.merge import LabelMergeError, merge_labels
from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label, label_conflict_message
from urbanlens.dashboard.services.undo.handlers.label import MODEL_LABEL as LABEL_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

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
    except (OSError, ValueError, PILDecompressionBombError):
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
        return Label.objects.user_labels().visible_to(profile).ordered().with_pin_counts()
    if kind == KIND_MEDIA:
        return Label.objects.media().visible_to(profile).ordered().with_pin_counts()
    msg = f"Unsupported label kind: {kind}"
    raise ValueError(msg)


def _auto_tag_available(user, profile: Profile, label_kind: str) -> bool:
    """Whether *profile* may auto-tag labels of this kind at all.

    One helper for both halves of the same decision: the edit form asks this to decide
    whether to show the per-label opt-out, and the save handler asks it to decide
    whether to honour the submitted value. Written out separately (as they were), the two
    can drift into rendering a control the server silently ignores, or ignoring one the
    server would have accepted.

    Auto-tagging is granted, not opted into: a user who has the capability and
    has not switched it off gets it for every tag and category label, minus
    whichever labels they excluded individually.

    Args:
        user: The requesting user, for the site-level AI feature check.
        profile: The owning profile, holding the per-kind preference flags.
        label_kind: The label's kind - only tags, categories and statuses are
            auto-taggable; anything else has no path and returns False.

    Returns:
        True when at least one auto-tagging path is available.
    """
    # Only tags and categories: REData's suggestion service models "which of my
    # labels describes this place", which statuses (visited, demolished) and
    # people/media labels are not.
    if label_kind not in {KIND_CATEGORY, KIND_TAG}:
        return False
    return bool(user_has_feature(user, SiteFeature.AUTO_TAGGING) and not profile.disable_auto_tagging)


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


def _would_create_cycle(label: Label, proposed_parent_id: int) -> bool:
    """Return True if adding ``proposed_parent_id`` as a parent of ``label`` would create a cycle.

    Thin wrapper kept for this module's many call sites (which check one
    candidate at a time, in both the parent and the child direction). The
    implementation now lives in ``services.labels.hierarchy`` so the external
    API's label write paths can enforce the same guard - see that module for
    why an unguarded ``parents`` write is a denial-of-service vector.

    Args:
        label: The label that would receive ``proposed_parent_id`` as a parent
            (or, when checking a child assignment, the label being added as a
            child - see call sites, which check both directions).
        proposed_parent_id: Primary key of the label proposed as a parent.

    Returns:
        True if the assignment would make ``label`` its own ancestor.
    """
    return would_create_cycle(label, [proposed_parent_id])


def _rows_ctx(kind: str, profile: Profile, can_edit_global: bool = False, extra: dict | None = None) -> dict:
    """Build template context for organize_label_rows.html and standalone index pages."""
    cfg = _config(kind)
    # Materialised before priming, and the same list is handed to the template:
    # priming seeds a memo on each instance, so a queryset re-evaluated during
    # rendering would discard it and quietly restore the per-label BFS.
    label_list = list(_queryset_for_kind(kind, profile))
    Label.prime_total_pin_counts(label_list)
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
        # Through clean_icon like the create and edit paths: truncating alone
        # fixed the over-long-value 500 but still stored free text as an icon.
        "icon": clean_icon(data.get("icon"), max_length=column_max_length(Label, "icon")),
        "color": clean_color(data.get("color")),
        "description": data.get("description", ""),
        "order": _safe_int(data.get("order"), 0),
        # int() over a client-supplied list raises ValueError on any non-numeric entry;
        # unparseable ids are dropped rather than failing the whole request.
        "add_parent_ids": [i for i in (_safe_int(x, -1) for x in data.get("add_parent_ids", [])) if i >= 0],
        "add_child_ids": [i for i in (_safe_int(x, -1) for x in data.get("add_child_ids", [])) if i >= 0],
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


def _validated_custom_icon(request: HttpRequest) -> tuple[Any, str | None]:
    """The submitted icon, checked and resized, or the reason it was refused.

    **Every path that stores a label icon must go through this.** The edit view
    validated its upload and the create view did not, so the same file that was
    refused with a 400 on one URL was written to disk from the other - a
    scripted SVG among them, since ``_resize_custom_icon`` deliberately returns
    the file untouched when PIL cannot open it, and ``label_icons/`` is served
    to any authenticated user with a Content-Type nginx derives from the
    extension.

    Args:
        request: The submitted request.

    Returns:
        ``(icon, None)`` when there is a usable icon (or ``(None, None)`` when
        none was submitted), or ``(None, message)`` when the upload failed a
        size/content-type/malware check.
    """
    custom_icon = _uploaded_custom_icon(request)
    if not custom_icon:
        return None, None

    from urbanlens.dashboard.models.images.model import MediaKind
    from urbanlens.dashboard.services.media.images import image_upload_error

    upload_error = image_upload_error(custom_icon, MediaKind.PHOTO)
    if upload_error:
        return None, upload_error[0]
    return _resize_custom_icon(custom_icon), None


def _apply_custom_icon_from_post(label: Label, request: HttpRequest) -> tuple[bool, str | None]:
    """Update label custom_icon from POST (upload or clear).

    Returns:
        A tuple of (whether custom_icon was actually touched, a user-facing
        error message if the uploaded icon failed a size/content-type/malware
        check - the icon is left unchanged in that case).
    """
    custom_icon, error = _validated_custom_icon(request)
    if error:
        return False, error
    if custom_icon:
        label.custom_icon = custom_icon
        return True, None
    if request.POST.get("clear_custom_icon"):
        # See achievements' equivalent: clearing the field does not remove the
        # stored file, so an explicitly-removed icon stayed fetchable.
        if label.custom_icon:
            label.custom_icon.delete(save=False)
        label.custom_icon = None
        return True, None
    return False, None


def _apply_kind_conversion(label: Label, new_kind: str, profile: Profile) -> bool:
    """Apply a kind change to a label. Returns True if kind changed."""
    if new_kind not in _ORGANIZE_KINDS or new_kind == label.kind:
        return False
    label.kind = new_kind
    if new_kind in (KIND_STATUS, KIND_CATEGORY):
        # Category, like Status, is always profile-scoped: _queryset_for_kind()
        # looks categories up via .for_profile() (exact match, no global
        # fallback), so a converted label left with profile=None would vanish
        # from every Organize > Categories listing and become permanently
        # un-editable (_can_modify_label() requires a non-None profile for
        # any non-tag kind). Assign it to the requesting profile, matching how
        # category labels are always created with a profile in LabelCreateView.
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
        name_error = column_length_error(Label, "name", name, cfg.singular_title)
        if name_error:
            return HttpResponse(name_error, status=400)

        parent_ids = request.POST.getlist("parent_ids")
        order = safe_int(request.POST.get("order"))
        parent_order = Label.initial_order_for_parents(profile, parent_ids)
        if parent_order is not None:
            order = parent_order

        # Checked before the insert so a collision is a 400 the form can show,
        # not the IntegrityError the database would raise (a 500 to the user).
        conflict = find_conflicting_label(profile=profile, name=name, kind=self.kind)
        if conflict is not None:
            # conflict.name is user-supplied (the colliding label's own name); this response is raw
            # text/html, not a Template, so it isn't auto-escaped - escape() matches the pattern used
            # for label.name elsewhere in this file (see LabelDeleteView, LabelBulkConvertView).
            return HttpResponse(escape(label_conflict_message(conflict, singular_title=cfg.singular_title)), status=400)

        custom_icon, icon_error = _validated_custom_icon(request)
        if icon_error:
            return HttpResponse(icon_error, status=400)

        label = Label.objects.create(
            kind=self.kind,
            profile=profile,
            name=name,
            description=request.POST.get("description", "").strip() or None,
            icon=clean_icon(request.POST.get("icon"), max_length=column_max_length(Label, "icon")) or None,
            color=clean_color(request.POST.get("color"), default=DEFAULT_LABEL_COLOR),
            custom_icon=custom_icon,
            order=order,
        )
        if parent_ids:
            valid_parents = _parent_candidates(profile, self.kind).filter(id__in=parent_ids).exclude(id=label.id)
            safe_parent_ids = [p.id for p in valid_parents if not _would_create_cycle(label, p.id)]
            label.parents.set(safe_parent_ids)

        child_ids = request.POST.getlist("child_ids")
        if child_ids:
            valid_children = _parent_candidates(profile, self.kind).filter(id__in=child_ids).exclude(id=label.id)
            for child in valid_children:
                if not _would_create_cycle(child, label.id):
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

        can_use_ai_features = user_has_feature(request.user, SiteFeature.AI)
        show_auto_tag_toggle = _auto_tag_available(request.user, profile, label.kind)

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
                "can_use_ai_features": can_use_ai_features,
                # Auto-tagging toggle needs either path to actually be able to assign
                # this label kind - the AI site feature plus the user's own master +
                # per-kind AI settings, OR the user's own keyword-tagging master +
                # per-kind settings (keyword matching needs no site feature/subscription).
                # Otherwise the option is offering a behavior the user has explicitly
                # turned off, or that isn't available to them at all.
                "show_auto_tag_toggle": show_auto_tag_toggle,
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

        # Scoped to only the fields this form actually edits, so a bare save()
        # never reverts a field changed concurrently by another request (e.g.
        # the external API's LabelDetailView.patch, which can touch fields -
        # keywords - this form has no control for at all).
        changed_fields = ["description", "icon", "color", "order"]

        if not label.is_protected:
            name = request.POST.get("name", "").strip()
            if not name:
                return HttpResponse("Name is required.", status=400)
            # exclude_pk so renaming a label to its own name (or just changing its
            # case) is not reported as colliding with itself.
            conflict = find_conflicting_label(profile=profile, name=name, kind=new_kind, exclude_pk=label.pk)
            if conflict is not None:
                # See LabelCreateView.post above: raw text/html HttpResponse, so
                # conflict.name (user-supplied) needs explicit escaping here.
                return HttpResponse(escape(label_conflict_message(conflict, singular_title=self._cfg().singular_title)), status=400)
            name_error = column_length_error(Label, "name", name, self._cfg().singular_title)
            if name_error:
                return HttpResponse(name_error, status=400)
            label.name = name
            changed_fields.append("name")

        label.description = request.POST.get("description", "").strip() or None
        # Through clean_icon, like the create path: truncating to the column width
        # fixed the over-long-icon 500 but still let arbitrary free text be stored
        # as an icon here while create rejected it.
        label.icon = clean_icon(request.POST.get("icon"), max_length=column_max_length(Label, "icon")) or None
        label.color = clean_color(request.POST.get("color"))
        label.order = safe_int(request.POST.get("order"), label.order)

        # allow_auto_tag can only be changed when the user actually has some auto-tagging
        # path available for this label's kind (AI or keyword-based); and never on the
        # protected "Visited" label.
        if not label.is_protected:
            can_toggle_auto_tag = _auto_tag_available(request.user, profile, label.kind)
            if can_toggle_auto_tag:
                # The form asks the question the other way round now: the
                # control is "exclude this label", so its absence means the
                # label participates.
                label.allow_auto_tag = "disable_auto_tag" not in request.POST
                changed_fields.append("allow_auto_tag")

        icon_changed, icon_error = _apply_custom_icon_from_post(label, request)
        if icon_error:
            return HttpResponse(icon_error, status=400)
        if icon_changed:
            changed_fields.append("custom_icon")

        kind_changed = _apply_kind_conversion(label, new_kind, profile)
        if kind_changed:
            changed_fields.extend(["kind", "profile"])
        label.save(update_fields=changed_fields)

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
            safe_parent_ids = [p.id for p in valid_parents if not _would_create_cycle(label, p.id)]
            label.parents.set(safe_parent_ids)

            child_ids = request.POST.getlist("child_ids")
            valid_children = _parent_candidates(profile, self.kind).filter(id__in=child_ids).exclude(id=label_id)
            safe_child_ids = [c.id for c in valid_children if not _would_create_cycle(c, label_id)]
            label.children.set(safe_child_ids)

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

        stash_for_undo(LABEL_MODEL_LABEL, [label], _request_profile(request))
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
        # Later duplicates win, matching the per-row loop this replaces.
        desired = {label_id: total - i for i, label_id in enumerate(label_ids)}

        # Filtering on profile/kind here is what keeps ids the caller does not own out
        # of the write - the per-row form got that from re-filtering inside the loop.
        # Only rows whose order actually moves are written or invalidated - the cache
        # refresh below costs work per *pin* carrying the label, so re-sending an
        # unchanged order would rebuild the whole map for nothing.
        labels = [label for label in Label.objects.filter(id__in=desired, profile=profile, kind=self.kind) if label.order != desired[label.pk]]
        for label in labels:
            label.order = desired[label.pk]
        if labels:
            Label.objects.bulk_update(labels, ["order"])
            # order decides which label supplies a pin's map icon/colour
            # (_winning_display_label sorts by -order), and bulk_update fires no
            # post_save, so the usual label -> cache receiver never sees this write.
            refresh_map_pin_cache_for_label_ids([label.pk for label in labels])
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

        try:
            merge_labels(target=target, sources=[source], profile=profile)
        except LabelMergeError as exc:
            return HttpResponse(exc.safe_message, status=400)

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

        # Merging *deletes* the source, so every guard that keeps a label from
        # being deleted has to hold here too. The single-merge view refuses a
        # protected source for every kind; this path only did for statuses,
        # which let a protected tag/category/person/media label be merged away.
        source_list = [label for label in sources if not label.is_protected]
        if not source_list:
            return HttpResponse(f"No valid source {self.kind}s.", status=400)

        try:
            merge_labels(target=target, sources=source_list, profile=profile)
        except LabelMergeError as exc:
            return HttpResponse(exc.safe_message, status=400)

        return _render_rows(request, self.kind, profile)


class LabelBulkDeleteView(_LabelKindMixin, LoginRequiredMixin, View):
    """Bulk-delete user-owned labels (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        ids, err = _parse_ids_json(request)
        if err:
            return err

        profile = _request_profile(request)
        # Protection is a property of the label, not of its kind - the single
        # delete view checks it for every kind, so this one must too.
        qs = Label.objects.filter(id__in=ids, profile=profile, kind=self.kind, is_protected=False)
        doomed = list(qs)
        if doomed:
            stash_for_undo(LABEL_MODEL_LABEL, doomed, profile)
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
        changed_labels = []
        for label in labels:
            update_fields = _apply_bulk_fields(label, payload)
            if update_fields:
                label.save(update_fields=update_fields)
                changed_labels.append(label)

        if changed_labels:
            # Bumping the label alone (its own post_save signal refreshes the
            # server-side map pin cache) isn't enough - the client's own pin
            # cache only refetches when Max(Pin.updated) advances, and this
            # bulk path never touches a Pin row directly. Same pattern as the
            # single-label edit/customize views below.
            Pin.objects.filter(profile=profile, labels__in=changed_labels).update(updated=timezone.now())

        if payload["add_parent_ids"]:
            # Scoped via _parent_candidates() (not a raw Label.objects.visible_to()
            # query) so this bulk path enforces the same KIND_USER/KIND_MEDIA
            # isolation as single create/edit.
            valid_parents = list(_parent_candidates(profile, self.kind).filter(id__in=payload["add_parent_ids"]))
            for label in labels:
                safe_parents = [p for p in valid_parents if p.id != label.id and not _would_create_cycle(label, p.id)]
                if safe_parents:
                    label.parents.add(*safe_parents)

        if payload["add_child_ids"]:
            valid_children = list(_parent_candidates(profile, self.kind).filter(id__in=payload["add_child_ids"]))
            for child in valid_children:
                safe_labels = [b for b in labels if b.id != child.id and not _would_create_cycle(child, b.id)]
                if safe_labels:
                    child.parents.add(*safe_labels)

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
        # Scoped via _parent_candidates() (not a raw Label.objects.visible_to()
        # query) so this bulk path enforces the same KIND_USER/KIND_MEDIA
        # isolation as single create/edit.
        # Label is unique on (lower(name), profile, kind), so a name that already exists in
        # the destination kind makes the save below a constraint violation - a 500 rather
        # than the readable refusal the single-edit path gives for the same collision.
        # Checked for the whole batch first: converting some and failing on others would
        # leave the user to work out which half applied.
        conflicts = [label for label in labels if find_conflicting_label(profile=profile, name=label.name, kind=new_kind, exclude_pk=label.pk) is not None]
        if conflicts:
            names = ", ".join(sorted(f'"{escape(label.name)}"' for label in conflicts))
            return HttpResponse(
                f"Cannot convert {names} - a {_config(new_kind).singular_title.lower()} with that name already exists. Rename or merge first.",
                status=400,
            )

        valid_parents = list(_parent_candidates(profile, self.kind).filter(id__in=payload["add_parent_ids"])) if payload["add_parent_ids"] else []
        for label in labels:
            _apply_bulk_fields(label, payload)
            label.kind = new_kind
            if new_kind == KIND_STATUS:
                label.profile = profile
            label.parents.clear()
            label.save()
            if valid_parents:
                safe_parents = [p for p in valid_parents if p.id != label.id and not _would_create_cycle(label, p.id)]
                if safe_parents:
                    label.parents.add(*safe_parents)

        if labels:
            # See LabelBulkEditView.post - the client's pin cache only refetches
            # when Max(Pin.updated) advances, and this bulk path never touches a
            # Pin row directly.
            Pin.objects.filter(profile=profile, labels__in=labels).update(updated=timezone.now())

        if payload["add_child_ids"]:
            valid_children = list(_parent_candidates(profile, self.kind).filter(id__in=payload["add_child_ids"]))
            for child in valid_children:
                safe_labels = [b for b in labels if b.id != child.id and not _would_create_cycle(child, b.id)]
                if safe_labels:
                    child.parents.add(*safe_labels)

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

        # Both branches nudge Pin.updated for this profile's pins - a
        # customization changes how they render on the map without touching
        # any Pin row, so the map cache's freshness check needs telling.
        if request.POST.get("action") == "clear":
            clear_label_customization(profile, label)
        else:
            upsert_label_customization(
                profile,
                label,
                name=request.POST.get("name", ""),
                icon=request.POST.get("icon"),
                color=clean_color(request.POST.get("color")),
            )

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
    dialog_only: bool = False,
) -> dict:
    """Build template context for label_membership_panel.html.

    Args:
        dialog_only: Skip the header and applied-labels chip list entirely,
            rendering just the add-label dialog inside the (invisible)
            ``panel_id`` wrapper - for call sites that only ever want the
            dialog (e.g. the photo gallery's label icon swaps into a bare
            slot div meant to hold nothing but the dialog) rather than a
            persistent visible panel.
    """
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
        "dialog_only": dialog_only,
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
        from urbanlens.dashboard.services.labels.redata_suggestions import redata_labels_configured

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
        ctx["pin_lists"] = list(PinList.objects.for_profile(profile).order_by("name"))
        # Lazily loaded (see label.pin_suggestions) rather than fetched here -
        # a live REData call has no business blocking this panel's own render.
        ctx["redata_labels_enabled"] = redata_labels_configured()
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
            # Tombstone first: keyword/AI auto-tagging can otherwise silently
            # reattach this exact label the next time it runs on this pin.
            PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.LABEL, value=str(label.pk))
            pin.labels.remove(label)
        return render(
            request,
            _MEMBERSHIP_PANEL,
            self._ctx(profile, pin, pin_slug),
        )


class LabelPinSuggestionsView(LoginRequiredMixin, View):
    """REData-suggested tag/category labels for a pin (HTMX, lazily loaded inside the Add Labels dialog)."""

    def get(self, request: HttpRequest, pin_slug: str, *args, **kwargs) -> HttpResponse:
        from urbanlens.dashboard.services.labels.redata_suggestions import get_suggestions

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        suggestions = get_suggestions(pin) or []
        member_ids = _pin_member_ids(pin)
        rows = [(label, round(confidence * 100)) for label, confidence in suggestions if label.id not in member_ids]
        return render(
            request,
            "dashboard/partials/labels/_label_suggestions.html",
            {
                "pin_slug": pin_slug,
                "suggestions": rows,
                "label_url_kind": _MEMBERSHIP_URL_KIND,
                "panel_id": "category-panel",
            },
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
            # Tombstone first: keyword/AI auto-tagging can otherwise silently
            # reattach this exact label the next time it runs on this wiki.
            WikiAutoRemoval.objects.record(wiki=wiki, kind=AutoRemovalKind.LABEL, value=str(label.pk))
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

    ``?embed=lightbox`` (or POST ``embed=lightbox``) renders the inline picker
    used in the photo lightbox rather than the gallery's add-label dialog.
    ``action=create_and_add`` creates a media label from ``name`` (or reuses
    an existing one of that name) and applies it in one step.
    """

    _LIGHTBOX = "dashboard/partials/labels/_lightbox_media_labels.html"

    def _get_owned_image(self, request: HttpRequest, image_uuid: str) -> Image:
        return get_object_or_404(Image, uuid=image_uuid, profile__user=request.user)

    def _template(self, request: HttpRequest) -> str:
        embed = request.POST.get("embed") or request.GET.get("embed")
        return self._LIGHTBOX if embed == "lightbox" else _MEMBERSHIP_PANEL

    def _ctx(self, profile, image: Image) -> dict:
        return _membership_panel_ctx(
            profile,
            _image_member_ids(image),
            panel_id="media-label-panel",
            dialog_id_prefix="media-label-dialog-",
            dialog_id_suffix=str(image.uuid),
            membership_route="image",
            obj_uuid=str(image.uuid),
            collapse_scope="image",
            empty_text="No media labels. Click + to add one.",
            labels_override=Label.objects.visible_to(profile).media().ordered(),
            dialog_only=True,
        )

    def _render(self, request: HttpRequest, profile, image: Image) -> HttpResponse:
        return render(request, self._template(request), self._ctx(profile, image))

    def _label_from_create(self, request: HttpRequest, profile) -> Label | HttpResponse:
        """Create a media label from ``name``, or return the existing one of that name."""
        name = (request.POST.get("name") or "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)
        name_error = column_length_error(Label, "name", name, "Media label")
        if name_error:
            return HttpResponse(name_error, status=400)
        conflict = find_conflicting_label(profile=profile, name=name, kind=KIND_MEDIA)
        if conflict is not None:
            return conflict
        return Label.objects.create(
            kind=KIND_MEDIA,
            profile=profile,
            name=name,
            color=clean_color(None, default=DEFAULT_LABEL_COLOR),
        )

    def get(self, request: HttpRequest, image_uuid: str, *args, **kwargs) -> HttpResponse:
        image = self._get_owned_image(request, image_uuid)
        profile = _request_profile(request)
        return self._render(request, profile, image)

    def post(self, request: HttpRequest, image_uuid: str, *args, **kwargs) -> HttpResponse:
        image = self._get_owned_image(request, image_uuid)
        profile = _request_profile(request)
        action = request.POST.get("action")
        if action == "create_and_add":
            label = self._label_from_create(request, profile)
            if isinstance(label, HttpResponse):
                return label
            image.labels.add(label)
            return self._render(request, profile, image)

        label_id = _membership_label_id(request)
        label = get_object_or_404(Label.objects.visible_to(profile), id=label_id, kind=KIND_MEDIA)
        if action == "add":
            image.labels.add(label)
        elif action == "remove":
            image.labels.remove(label)
        return self._render(request, profile, image)
