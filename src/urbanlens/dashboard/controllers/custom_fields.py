"""Custom field views - define per-user fields and edit their values on targets.

Field definitions are managed from Settings > Customize (and inline from the pin
detail panel). Values are edited wherever the target is displayed: the pin detail
page, another user's profile page, and the photo / markup-map lightboxes.

Everything here is strictly private to the requesting user: fields are looked up
through ``profile=request.user.profile`` and targets are checked for ownership
(pins, photos, maps) before a value is written.
"""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.custom_fields.model import (
    ENTITY_ICONS,
    FIXED_POS_MAX_LEFT,
    FIXED_POS_MAX_TOP,
    STYLES_BY_TYPE,
    CustomField,
    CustomFieldDisplay,
    CustomFieldEntity,
    CustomFieldType,
    CustomFieldValue,
)
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.custom_field_references import REFERENCE_KINDS

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


def _show_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    """Attach a showToast HX-Trigger to a response.

    Args:
        response: The response to annotate.
        message: Toast message text.
        level: toastr level (``success``, ``info``, ``warning``, ``error``).

    Returns:
        The same response, with the trigger header merged in.
    """
    triggers = json.loads(response.headers.get("HX-Trigger", "{}")) if response.headers.get("HX-Trigger") else {}
    triggers["showToast"] = {"level": level, "message": message}
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _profile_for(request: HttpRequest) -> Profile:
    """Return (creating if needed) the requesting user's profile."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def rows_for_target(profile: Profile, entity_type: str, target: Any) -> list[dict[str, Any]]:
    """Build display rows pairing each of the profile's fields with its value on a target.

    Args:
        profile: The field owner (the requesting user).
        entity_type: A :class:`CustomFieldEntity` value.
        target: The Pin / Image / Profile / MarkupMap the values are attached to.

    Returns:
        List of ``{"field": CustomField, "value": CustomFieldValue | None}`` dicts
        in field display order.
    """
    fields = list(CustomField.objects.for_entity(profile, entity_type))
    values_by_field_id = {v.field_id: v for v in CustomFieldValue.objects.filter(field__in=fields).for_target(target).select_related("field")}
    rows: list[dict[str, Any]] = []
    for field in fields:
        value = values_by_field_id.get(field.pk)
        row: dict[str, Any] = {"field": field, "value": value}
        if field.field_type == CustomFieldType.REFERENCE:
            row["ref_choices"] = field.reference_choices(include_pk=value.reference_pk if value else None)
        rows.append(row)
    return rows


def save_value(field: CustomField, target: Any, raw: str) -> tuple[CustomFieldValue | None, str | None]:
    """Create, update, or clear the value of ``field`` on ``target``.

    An empty ``raw`` deletes the stored value (the field simply has no value on
    that target anymore).

    Args:
        field: The field being written. Must match the target's entity type.
        target: The Pin / Image / Profile / MarkupMap instance.
        raw: The user-entered value.

    Returns:
        Tuple of (saved value or None when cleared, error message or None).
    """
    target_attr = CustomFieldValue.TARGET_FIELD_BY_ENTITY.get(field.entity_type)
    if target_attr is None:
        return None, "Unsupported field type."

    existing = CustomFieldValue.objects.filter(field=field).for_target(target).first()
    raw = (raw or "").strip()
    if not raw:
        if existing is not None:
            existing.delete()
        return None, None

    value = existing or CustomFieldValue(field=field, **{target_attr: target})
    try:
        value.set_value(raw)
    except ValueError as e:
        return None, str(e)
    value.save()
    return value, None


#: Hard cap on select options, to keep dropdowns (and the config JSON) sane.
_MAX_SELECT_OPTIONS = 100


def _parse_options(raw: str) -> tuple[list[str], str | None]:
    """Parse a select field's options from newline/comma-separated text.

    Args:
        raw: The raw ``options`` POST value.

    Returns:
        Tuple of (deduplicated option list, error message or None).
    """
    pieces = [piece.strip() for chunk in raw.splitlines() for piece in chunk.split(",")]
    options: list[str] = []
    for piece in pieces:
        if not piece or piece in options:
            continue
        if len(piece) > 100:
            return [], "Options must be 100 characters or less."
        options.append(piece)
    if not options:
        return [], "Select fields need at least one option."
    if len(options) > _MAX_SELECT_OPTIONS:
        return [], f"Too many options ({_MAX_SELECT_OPTIONS} max)."
    return options, None


def _parse_definition(request: HttpRequest) -> tuple[dict[str, Any], str | None]:
    """Extract and validate a field definition from a create/update POST.

    Reads ``name``, ``field_type``, ``style``, ``options`` (select fields), and
    ``slider_min``/``slider_max`` (slider style), validating each against the
    chosen type.

    Returns:
        Tuple of (definition dict with ``name``/``field_type``/``style``/``config``
        keys, error message or None). The dict is empty when there's an error.
    """
    name = (request.POST.get("name") or "").strip()
    field_type = (request.POST.get("field_type") or "").strip() or CustomFieldType.TEXT
    style = (request.POST.get("style") or "").strip()
    display = (request.POST.get("display") or "").strip() or CustomFieldDisplay.DEFAULT
    if not name:
        return {}, "Name is required."
    if len(name) > 100:
        return {}, "Name is too long (100 characters max)."
    if field_type not in CustomFieldType.values:
        return {}, "Invalid field type."
    if display not in CustomFieldDisplay.values:
        return {}, "Invalid display option."

    allowed_styles = [value for value, _ in STYLES_BY_TYPE.get(field_type, [])]
    if style and style not in allowed_styles:
        return {}, "That style doesn't apply to this field type."

    config: dict[str, Any] = {}
    if field_type == CustomFieldType.SELECT:
        options, error = _parse_options(request.POST.get("options") or "")
        if error:
            return {}, error
        config["choices"] = options
    if field_type == CustomFieldType.REFERENCE:
        ref_type = (request.POST.get("ref_type") or "").strip()
        if not any(ref_type == kind for kind, _ in REFERENCE_KINDS):
            return {}, "Choose what this reference field points at."
        config["ref_type"] = ref_type
    if style == "slider":
        bounds: dict[str, float] = {}
        for key in ("slider_min", "slider_max"):
            raw_bound = (request.POST.get(key) or "").strip()
            if not raw_bound:
                continue
            try:
                parsed_bound = float(raw_bound)
            except ValueError:
                return {}, "Slider bounds must be numbers."
            if not math.isfinite(parsed_bound):
                return {}, "Slider bounds must be numbers."
            bounds[key.removeprefix("slider_")] = parsed_bound
        if "min" in bounds and "max" in bounds and bounds["min"] >= bounds["max"]:
            return {}, "The slider minimum must be less than its maximum."
        config.update(bounds)

    return {"name": name, "field_type": field_type, "style": style, "display": display, "config": config}, None


def create_field(profile: Profile, entity_type: str, request: HttpRequest) -> str | None:
    """Create a custom field from POST data, returning an error message on failure.

    Args:
        profile: The owning profile.
        entity_type: A :class:`CustomFieldEntity` value.
        request: POST carrying ``name``, ``field_type``, and optionally
            ``style``/``options``/``slider_min``/``slider_max``.

    Returns:
        None on success, or a user-facing error message.
    """
    if entity_type not in CustomFieldEntity.values:
        return "Invalid entity type."
    definition, error = _parse_definition(request)
    if error:
        return error
    if CustomField.objects.filter(profile=profile, entity_type=entity_type, name__iexact=definition["name"]).exists():
        return f"You already have a “{definition['name']}” field there."
    try:
        CustomField.objects.create(profile=profile, entity_type=entity_type, **definition)
    except IntegrityError:
        return f"You already have a “{definition['name']}” field there."
    return None


# -- Settings > Customize -----------------------------------------------------


class CustomFieldSettingsPanelView(LoginRequiredMixin, View):
    """GET: the Settings > Customize panel.  POST: create a new field."""

    def _render_panel(self, request: HttpRequest, profile: Profile, error: str | None = None) -> HttpResponse:
        """Render the settings panel listing all fields grouped by entity type."""
        groups = []
        for entity_type, label in CustomFieldEntity.choices:
            groups.append(
                {
                    "entity_type": entity_type,
                    "label": label,
                    "icon": ENTITY_ICONS.get(entity_type, "tune"),
                    "fields": list(CustomField.objects.for_entity(profile, entity_type)),
                    # display placement only exists on the pin detail page today.
                    "supports_display": entity_type == CustomFieldEntity.PIN,
                },
            )
        response = render(
            request,
            "dashboard/partials/custom_fields/settings_panel.html",
            {
                "groups": groups,
                "field_types": CustomFieldType.choices,
                "styles_by_type": STYLES_BY_TYPE,
                "reference_kinds": REFERENCE_KINDS,
                "display_choices": CustomFieldDisplay.choices,
            },
        )
        if error:
            return _show_toast(response, error, level="error")
        return response

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._render_panel(request, _profile_for(request))

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _profile_for(request)
        entity_type = (request.POST.get("entity_type") or "").strip()
        error = create_field(profile, entity_type, request)
        return self._render_panel(request, profile, error=error)


class CustomFieldUpdateView(LoginRequiredMixin, View):
    """POST: rename a field and/or change its type (type changes require no stored values)."""

    def post(self, request: HttpRequest, field_id: int) -> HttpResponse:
        profile = _profile_for(request)
        field = get_object_or_404(CustomField, id=field_id, profile=profile)
        definition, error = _parse_definition(request)
        name = definition.get("name", "")
        if error is None and definition["field_type"] != field.field_type and field.values.exists():
            error = "This field already has values - clear them before changing its type."
        if error is None and field.field_type == CustomFieldType.REFERENCE and definition["field_type"] == CustomFieldType.REFERENCE and definition["config"].get("ref_type") != field.reference_kind and field.values.exists():
            error = "This field already has values - clear them before changing what it references."
        if error is None and name.lower() != field.name.lower() and CustomField.objects.filter(profile=profile, entity_type=field.entity_type, name__iexact=name).exclude(pk=field.pk).exists():
            error = f"You already have a “{name}” field there."
        if error is None:
            field.name = name
            field.field_type = definition["field_type"]
            field.style = definition["style"]
            field.display = definition["display"]
            # Keep any config keys the form doesn't manage, but replace the managed ones.
            # (fixed_pos survives here on purpose - a dragged position outlives edits.)
            config = dict(field.config or {})
            for key in ("choices", "min", "max", "ref_type"):
                config.pop(key, None)
            config.update(definition["config"])
            field.config = config
            try:
                field.save(update_fields=["name", "field_type", "style", "display", "config", "updated"])
            except IntegrityError:
                error = f"You already have a “{name}” field there."
        return CustomFieldSettingsPanelView()._render_panel(request, profile, error=error)  # noqa: SLF001


class CustomFieldDeleteView(LoginRequiredMixin, View):
    """DELETE: remove a field definition and every value stored under it."""

    def delete(self, request: HttpRequest, field_id: int) -> HttpResponse:
        profile = _profile_for(request)
        field = get_object_or_404(CustomField, id=field_id, profile=profile)
        field_name = field.name
        field.delete()
        response = CustomFieldSettingsPanelView()._render_panel(request, profile)  # noqa: SLF001
        return _show_toast(response, f"Deleted custom field “{field_name}”.")


# -- Pin detail panel ----------------------------------------------------------


#: Default placement (viewport %) for fixed fields that were never dragged:
#: stacked down the right edge, offset per field so they don't overlap.
_FIXED_DEFAULT_LEFT = 72.0
_FIXED_DEFAULT_TOP = 16.0
_FIXED_DEFAULT_STEP = 11.0


def _render_pin_panel(request: HttpRequest, profile: Profile, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's custom-fields area.

    The response is one wrapper (``#pin-custom-fields-panel``) holding the
    Custom Fields card (display=default rows), one standalone card per
    display=section field, and one draggable overlay per display=fixed field -
    so every value save or definition change re-renders all three placements
    consistently in a single swap.
    """
    rows = rows_for_target(profile, CustomFieldEntity.PIN, pin)
    default_rows = [row for row in rows if row["field"].display == CustomFieldDisplay.DEFAULT]
    section_rows = [row for row in rows if row["field"].display == CustomFieldDisplay.SECTION]
    fixed_rows = [row for row in rows if row["field"].display == CustomFieldDisplay.FIXED]
    for index, row in enumerate(fixed_rows):
        position = row["field"].fixed_position or {
            "left": _FIXED_DEFAULT_LEFT,
            "top": min(_FIXED_DEFAULT_TOP + index * _FIXED_DEFAULT_STEP, float(FIXED_POS_MAX_TOP)),
        }
        # Pre-formatted so template locale settings can't mangle the floats.
        row["position_style"] = f"left: {position['left']:.2f}%; top: {position['top']:.2f}%;"
    response = render(
        request,
        "dashboard/partials/custom_fields/pin_panel.html",
        {
            "pin": pin,
            "rows": default_rows,
            "section_rows": section_rows,
            "fixed_rows": fixed_rows,
            "total_field_count": len(rows),
            "field_types": CustomFieldType.choices,
            "styles_by_type": STYLES_BY_TYPE,
            "reference_kinds": REFERENCE_KINDS,
            "display_choices": CustomFieldDisplay.choices,
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class PinCustomFieldsPanelView(LoginRequiredMixin, View):
    """GET: the pin's Custom Fields card.  POST: create a new pin field inline."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        profile = _profile_for(request)
        pin = get_object_or_404(Pin, slug=pin_slug, profile=profile)
        return _render_pin_panel(request, profile, pin)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        profile = _profile_for(request)
        pin = get_object_or_404(Pin, slug=pin_slug, profile=profile)
        error = create_field(profile, CustomFieldEntity.PIN, request)
        return _render_pin_panel(request, profile, pin, error=error)


class PinCustomFieldValueView(LoginRequiredMixin, View):
    """POST: set (or clear, with an empty value) one custom field's value on a pin."""

    def post(self, request: HttpRequest, pin_slug: str, field_id: int) -> HttpResponse:
        profile = _profile_for(request)
        pin = get_object_or_404(Pin, slug=pin_slug, profile=profile)
        field = get_object_or_404(CustomField, id=field_id, profile=profile, entity_type=CustomFieldEntity.PIN)
        _, error = save_value(field, pin, request.POST.get("value", ""))
        return _render_pin_panel(request, profile, pin, error=error)


class CustomFieldPositionView(LoginRequiredMixin, View):
    """POST: remember where the user dragged a fixed-display field.

    The position is per-field (fields are already per-user) and applies to
    every pin detail page, per the feature request. Values are viewport
    percentages, clamped server-side so a bad client can't park a field
    off-screen for good.
    """

    def post(self, request: HttpRequest, field_id: int) -> HttpResponse:
        profile = _profile_for(request)
        field = get_object_or_404(CustomField, id=field_id, profile=profile)
        try:
            body = json.loads(request.body or b"{}")
            left = float(body.get("left"))
            top = float(body.get("top"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return HttpResponse("Invalid position.", status=400)
        if not (math.isfinite(left) and math.isfinite(top)):
            return HttpResponse("Invalid position.", status=400)

        config = dict(field.config or {})
        config["fixed_pos"] = {
            "left": round(min(max(left, 0.0), float(FIXED_POS_MAX_LEFT)), 2),
            "top": round(min(max(top, 0.0), float(FIXED_POS_MAX_TOP)), 2),
        }
        field.config = config
        field.save(update_fields=["config", "updated"])
        return HttpResponse(status=204)


# -- Profile annotation values ---------------------------------------------------


class ProfileCustomFieldValueView(LoginRequiredMixin, View):
    """POST: set/clear a people-field value on another user's profile (viewer-private)."""

    def post(self, request: HttpRequest, profile_slug: str, field_id: int) -> HttpResponse:
        from urbanlens.dashboard.controllers.userprofile import _render_profile_annotation_partial

        author = _profile_for(request)
        subject = get_object_or_404(Profile, slug=profile_slug)
        if subject.pk == author.pk:
            return HttpResponse("You cannot annotate your own profile.", status=400)
        field = get_object_or_404(CustomField, id=field_id, profile=author, entity_type=CustomFieldEntity.PROFILE)
        _, error = save_value(field, subject, request.POST.get("value", ""))
        response = _render_profile_annotation_partial(request, author, subject)
        if error:
            return _show_toast(response, error, level="error")
        return response


# -- Lightbox strips (photos and markup maps) ------------------------------------


def _render_strip(request: HttpRequest, profile: Profile, entity_type: str, target: Any, url_name: str, target_arg: Any, error: str | None = None) -> HttpResponse:
    """Render the compact custom-fields strip shown under lightboxes.

    Returns 204 (so the strip stays hidden) when the user has no fields for
    this entity type.
    """
    rows = rows_for_target(profile, entity_type, target)
    if not rows:
        return HttpResponse(status=204)
    response = render(
        request,
        "dashboard/partials/custom_fields/lightbox_strip.html",
        {"rows": rows, "url_name": url_name, "target_arg": target_arg},
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class PhotoCustomFieldsView(LoginRequiredMixin, View):
    """GET: custom-fields strip for one of the user's photos.  POST: save a value."""

    def _resolve(self, request: HttpRequest, image_id: int) -> tuple[Profile, Image | None]:
        """Return (profile, image) - image is None when it isn't the user's own photo."""
        profile = _profile_for(request)
        image = Image.objects.filter(id=image_id, profile=profile).first()
        return profile, image

    def get(self, request: HttpRequest, image_id: int) -> HttpResponse:
        profile, image = self._resolve(request, image_id)
        if image is None:
            return HttpResponse(status=204)
        return _render_strip(request, profile, CustomFieldEntity.PHOTO, image, "custom_fields.photo", image.pk)

    def post(self, request: HttpRequest, image_id: int) -> HttpResponse:
        profile, image = self._resolve(request, image_id)
        if image is None:
            return HttpResponse(status=204)
        field = get_object_or_404(CustomField, id=request.POST.get("field_id"), profile=profile, entity_type=CustomFieldEntity.PHOTO)
        _, error = save_value(field, image, request.POST.get("value", ""))
        return _render_strip(request, profile, CustomFieldEntity.PHOTO, image, "custom_fields.photo", image.pk, error=error)


class MarkupMapCustomFieldsView(LoginRequiredMixin, View):
    """GET: custom-fields strip for one of the user's markup maps.  POST: save a value."""

    def _resolve(self, request: HttpRequest, map_uuid) -> tuple[Profile, MarkupMap | None]:
        """Return (profile, map) - map is None when it isn't the user's own map."""
        profile = _profile_for(request)
        markup_map = MarkupMap.objects.filter(uuid=map_uuid, profile=profile).first()
        return profile, markup_map

    def get(self, request: HttpRequest, map_uuid) -> HttpResponse:
        profile, markup_map = self._resolve(request, map_uuid)
        if markup_map is None:
            return HttpResponse(status=204)
        return _render_strip(request, profile, CustomFieldEntity.MARKUP_MAP, markup_map, "custom_fields.markup_map", markup_map.uuid)

    def post(self, request: HttpRequest, map_uuid) -> HttpResponse:
        profile, markup_map = self._resolve(request, map_uuid)
        if markup_map is None:
            return HttpResponse(status=204)
        field = get_object_or_404(CustomField, id=request.POST.get("field_id"), profile=profile, entity_type=CustomFieldEntity.MARKUP_MAP)
        _, error = save_value(field, markup_map, request.POST.get("value", ""))
        return _render_strip(request, profile, CustomFieldEntity.MARKUP_MAP, markup_map, "custom_fields.markup_map", markup_map.uuid, error=error)
