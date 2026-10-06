"""Multi-select bulk actions for pins (root or child) on the main map: merge, delete+undo, bulk edit."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User as AuthUser
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.core.colors import clean_color
from urbanlens.dashboard.services.core.icons import clean_icon
from urbanlens.dashboard.services.core.numbers import safe_int_or_none
from urbanlens.dashboard.services.core.request_body import list_field, posted_json_object
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.core.uuids import uuid_or_none, valid_uuids
from urbanlens.dashboard.services.pins.pin_bulk import MAX_BULK_PINS, UNSET, BulkPinEdit, BulkPinError, Unset, bulk_delete_pins, bulk_edit_pins, bulk_merge_under
from urbanlens.dashboard.services.undo.service import UndoExpiredError, restore_undo_action

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_ORGANIZE_KINDS = frozenset({KIND_TAG, KIND_CATEGORY, KIND_STATUS})

#: Shared wording so every bulk endpoint refuses identically.
_TOO_MANY_PINS = f"Select at most {MAX_BULK_PINS} pins at a time."


def _too_many(uuids: list[str]) -> bool:
    """Report whether a request named more pins than one call may carry."""
    return len(uuids) > MAX_BULK_PINS


def _request_profile(request: HttpRequest) -> Profile:
    """Return the authenticated user's Profile; raises if user is anonymous."""
    if not isinstance(request.user, AuthUser):
        raise TypeError("Expected an authenticated user")
    return request.user.profile


def _parse_uuids_json(request: HttpRequest, key: str = "uuids") -> tuple[list[str] | None, HttpResponse | None]:
    """Parse a JSON body containing a list of pin uuid strings under ``key``."""
    try:
        data = posted_json_object(request)
        uuids = [str(x) for x in list_field(data, key)]
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, JsonResponse({"error": "Invalid data"}, status=400)
    if not uuids:
        return None, HttpResponse("No pins specified.", status=400)
    if _too_many(uuids):
        return None, HttpResponse(_TOO_MANY_PINS, status=400)
    return uuids, None


def _posted_ids(data: Mapping[str, Any], key: str) -> list[int]:
    """The integer ids a JSON body lists under *key*, dropping any that are not one."""
    return [parsed for parsed in map(safe_int_or_none, list_field(data, key)) if parsed is not None]


def _owned_pins(profile: Profile, uuids: list[str]) -> QuerySet[Pin]:
    """Pins (root or child) owned by ``profile`` among the given uuids.

    The main map's select tool can select both root and child (sub) pin
    markers, so bulk actions must be able to resolve either kind.
    """
    return Pin.objects.filter(profile=profile, uuid__in=valid_uuids(uuids))


class PinBulkDeleteView(LoginRequiredMixin, View):
    """Delete selected root pins (and their full detail-pin subtree), staging an undo."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        uuids, err = _parse_uuids_json(request)
        if err:
            return err

        # Future proofing:guaranteed by _parse_uuids_json when err is None
        if uuids is None:
            return HttpResponse("No pins specified.", status=400)

        profile = _request_profile(request)
        pins = list(_owned_pins(profile, uuids))
        if not pins:
            return HttpResponse("No matching pins.", status=404)

        result = bulk_delete_pins(profile, pins)
        return JsonResponse(
            {
                "ok": True,
                "undo_token": str(result.undo_action.uuid),
                "count": len(result.deleted),
                "descendant_count": result.descendant_count,
                "total_count": len(result.subtree),
            }
        )


class PinBulkUndoView(LoginRequiredMixin, View):
    """Restore pins previously removed by ``PinBulkDeleteView``, within the undo grace period."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = posted_json_object(request)
            token = str(data.get("token") or "")
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)
        if not token:
            return HttpResponse("token is required.", status=400)

        profile = _request_profile(request)
        try:
            undo_action = UndoAction.objects.for_profile(profile).get(uuid=token, model_label="pin")
        except (UndoAction.DoesNotExist, ValueError, ValidationError):
            return JsonResponse({"ok": False, "error": "This undo has expired."}, status=410)

        try:
            restored = restore_undo_action(undo_action)
        except UndoExpiredError:
            return JsonResponse({"ok": False, "error": "This undo has expired."}, status=410)

        return JsonResponse({"ok": True, "restored": [{"uuid": str(p.uuid), "name": p.effective_name} for p in restored]})


class PinBulkMergeView(LoginRequiredMixin, View):
    """Merge selected pins: all but the target become the target's detail pins.

    The target becomes (or stays) the top-level pin; a target that's currently
    a child pin is promoted first (see the conflict check below).
    """

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = posted_json_object(request)
            target_uuid = str(data.get("target_uuid") or "")
            source_uuids = [str(x) for x in list_field(data, "source_uuids")]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not target_uuid:
            return HttpResponse("target_uuid is required.", status=400)
        if not source_uuids:
            return HttpResponse("At least one source_uuid is required.", status=400)
        # Before any database work: a request that is too large should not first pay for being too large.
        if _too_many(source_uuids):
            return HttpResponse(_TOO_MANY_PINS, status=400)

        profile = _request_profile(request)
        target = get_object_or_404(Pin.objects.filter(profile=profile), uuid=uuid_or_none(target_uuid))
        try:
            result = bulk_merge_under(target, list(_owned_pins(profile, source_uuids)))
        except BulkPinError as exc:
            return HttpResponse(exc.message, status=400)
        return JsonResponse({"ok": True, "merged": len(result.merged), "target_uuid": str(target.uuid)})


class PinBulkEditView(LoginRequiredMixin, View):
    """Bulk-edit shared content, organization, and marker style across selected pins."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = posted_json_object(request)
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        uuids = [str(x) for x in list_field(data, "uuids")]
        if not uuids:
            return HttpResponse("No pins specified.", status=400)
        if _too_many(uuids):
            return HttpResponse(_TOO_MANY_PINS, status=400)

        profile = _request_profile(request)
        pins = list(_owned_pins(profile, uuids))
        if not pins:
            return HttpResponse("No matching pins.", status=404)

        style_updates: dict[str, str | int | None] = {}
        for request_field, model_field, max_length in (
            ("icon", "icon", 255),
            ("color", "color", 20),
            ("bg_color", "detail_bg_color", 20),
            ("border_color", "detail_border_color", 20),
        ):
            if request_field not in data:
                continue
            raw_value = data[request_field]
            value = str(raw_value).strip() if raw_value is not None else ""
            if len(value) > max_length:
                return HttpResponse(f"{request_field} is too long.", status=400)
            if model_field.endswith("color"):
                # Length alone is not enough: these are interpolated into style="..." by the map and organize
                # renderers, and `x" onmouse` is ten characters.
                value = clean_color(value, default="", allow_none_keyword=model_field != "color") or ""
            elif model_field == "icon":
                # Same reasoning one branch over: an icon becomes glyph text, an <img src="...">, or an emoji
                # depending on its shape, so a value that is none of the three has no business being stored.
                value = clean_icon(value, default="")
            style_updates[model_field] = value or None

        for request_field, model_field in (
            ("bg_opacity", "detail_bg_opacity"),
            ("border_opacity", "detail_border_opacity"),
        ):
            if request_field not in data:
                continue
            int_value = safe_int_or_none(data[request_field])
            if int_value is None:
                return HttpResponse(f"{request_field} must be a percentage.", status=400)
            if not 0 <= int_value <= 100:
                return HttpResponse(f"{request_field} must be between 0 and 100.", status=400)
            style_updates[model_field] = int_value

        description = data.get("description")
        new_description: str | Literal[Unset.UNSET] = UNSET
        if isinstance(description, str) and description.strip():
            length_error = text_length_error(description, MAX_PIN_DESCRIPTION_LENGTH, "Description")
            if length_error:
                return HttpResponse(length_error, status=400)
            new_description = description

        # 0 clears every selected pin's review; absent or out of range leaves ratings alone.
        rating_raw = data.get("rating")
        rating: int | Literal[Unset.UNSET] | None = UNSET
        if rating_raw is not None and str(rating_raw).strip():
            parsed_rating = safe_int_or_none(rating_raw)
            if parsed_rating is not None and 1 <= parsed_rating <= 5:
                rating = parsed_rating
            elif parsed_rating == 0:
                rating = None

        add_labels: list[Label] = []
        if add_ids := _posted_ids(data, "add_label_ids"):
            add_labels = list(Label.objects.visible_to(profile).filter(id__in=add_ids, kind__in=_ORGANIZE_KINDS))

        remove_labels: list[Label] = []
        if remove_ids := _posted_ids(data, "remove_label_ids"):
            # Never trust the client's option list - only labels present on at least one selected pin.
            remove_labels = list(Label.objects.filter(id__in=remove_ids, kind__in=_ORGANIZE_KINDS, pins__in=pins).distinct())

        parent: Pin | Literal[Unset.UNSET] = UNSET
        parent_uuid = str(data.get("parent_uuid") or "").strip()
        if parent_uuid:
            parent = get_object_or_404(Pin.objects.filter(profile=profile), uuid=uuid_or_none(parent_uuid))

        result = bulk_edit_pins(
            profile,
            pins,
            BulkPinEdit(
                description=new_description,
                style=style_updates,
                rating=rating,
                add_labels=add_labels,
                remove_labels=remove_labels,
                parent=parent,
            ),
        )
        return JsonResponse({"ok": True, "count": result.count, "reparented": result.reparented})


class PinBulkEditLabelOptionsView(LoginRequiredMixin, View):
    """Return the union of organize labels present on at least one of the given pins."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        uuids = request.GET.getlist("uuids")
        if not uuids:
            return JsonResponse({"labels": []})

        profile = _request_profile(request)
        pins = _owned_pins(profile, uuids)
        labels = Label.objects.filter(kind__in=_ORGANIZE_KINDS, pins__in=pins).distinct().order_by("name")
        return JsonResponse(
            {
                "labels": [{"id": b.id, "name": b.name, "icon": b.effective_icon, "color": b.effective_color, "kind": b.kind} for b in labels],
            },
        )


class PinParentSearchView(LoginRequiredMixin, View):
    """Search the requester's own pins by name or alias, to pick a bulk-edit parent target.

    GET /map/pins/parent-search/?q=...&exclude=<uuid>&exclude=<uuid>...
    """

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        query = (request.GET.get("q") or "").strip()
        if len(query) < 2:
            return JsonResponse({"results": []})

        profile = _request_profile(request)
        exclude_uuids = set(valid_uuids(request.GET.getlist("exclude")))
        pins = Pin.objects.filter(profile=profile).select_related("location").filter(Q(name__icontains=query) | Q(aliases__name__icontains=query)).exclude(uuid__in=exclude_uuids).distinct().order_by("name")[:10]
        return JsonResponse(
            {
                "results": [
                    {
                        "uuid": str(pin.uuid),
                        "name": pin.effective_name,
                        "subtitle": pin.location.display_name if pin.location else "",
                    }
                    for pin in pins
                ],
            },
        )


class PinBulkExportView(LoginRequiredMixin, View):
    """Download selected pins as GeoJSON/KML/GPX/CSV (UL-377/UL-382, plain form POST).

    A plain (non-JSON) form POST, not fetch/JSON like the other bulk views - submitted via a throwaway
    <form target="_blank"> so the browser handles the file download itself from the Content-Disposition
    header, with no URL-length limit on the pin count and no client-side blob handling.
    """

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from urbanlens.dashboard.services.import_export.export_formats import EXPORT_FORMATS

        fmt = request.POST.get("format", "")
        if fmt not in EXPORT_FORMATS:
            return HttpResponse("Unknown export format.", status=400)

        uuids = [str(x) for x in request.POST.getlist("uuids")]
        if not uuids:
            return HttpResponse("No pins specified.", status=400)

        profile = _request_profile(request)
        pins = _owned_pins(profile, uuids).select_related("location")
        if not pins.exists():
            return HttpResponse("No matching pins.", status=404)

        writer, extension, content_type = EXPORT_FORMATS[fmt]
        content = writer(pins)
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="pins.{extension}"'
        return response
