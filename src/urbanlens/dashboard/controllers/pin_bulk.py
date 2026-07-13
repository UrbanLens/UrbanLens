"""Multi-select bulk actions for root pins on the main map: merge, delete+undo, bulk edit."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User as AuthUser
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.badges.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.text_limits import MAX_PIN_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.undo.service import UndoExpiredError, restore_undo_action, stash_for_undo

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_ORGANIZE_KINDS = frozenset({KIND_TAG, KIND_CATEGORY, KIND_STATUS})


def _request_profile(request: HttpRequest) -> Profile:
    """Return the authenticated user's Profile; raises if user is anonymous."""
    if not isinstance(request.user, AuthUser):
        raise TypeError("Expected an authenticated user")
    return request.user.profile


def _parse_uuids_json(request: HttpRequest, key: str = "uuids") -> tuple[list[str] | None, HttpResponse | None]:
    """Parse a JSON body containing a list of pin uuid strings under ``key``."""
    try:
        data = json.loads(request.body)
        uuids = [str(x) for x in data.get(key, [])]
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, JsonResponse({"error": "Invalid data"}, status=400)
    if not uuids:
        return None, HttpResponse("No pins specified.", status=400)
    return uuids, None


def _owned_root_pins(profile: Profile, uuids: list[str]) -> QuerySet[Pin]:
    """Root pins owned by ``profile`` among the given uuids.

    Scoped to root pins because the main map's select tool only ever shows
    root pins as markers - detail pins are never a valid bulk-action target.
    """
    return Pin.objects.filter(profile=profile, uuid__in=uuids).root_pins()


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
        pins = list(_owned_root_pins(profile, uuids))
        if not pins:
            return HttpResponse("No matching pins.", status=404)

        subtree = list(Pin.objects.filter(pk__in=[p.pk for p in pins]).with_descendants())
        undo_action = stash_for_undo("pin", subtree, profile)
        for pin in subtree:
            pin.delete()

        return JsonResponse({"ok": True, "undo_token": str(undo_action.uuid), "count": len(pins)})


class PinBulkUndoView(LoginRequiredMixin, View):
    """Restore pins previously removed by ``PinBulkDeleteView``, within the undo grace period."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = json.loads(request.body)
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
    """Merge selected root pins: all but the target become the target's detail pins."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = json.loads(request.body)
            target_uuid = str(data.get("target_uuid") or "")
            source_uuids = [str(x) for x in data.get("source_uuids", [])]
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not target_uuid:
            return HttpResponse("target_uuid is required.", status=400)
        if not source_uuids:
            return HttpResponse("At least one source_uuid is required.", status=400)

        profile = _request_profile(request)
        target = get_object_or_404(Pin.objects.root_pins(), uuid=target_uuid, profile=profile)
        sources = list(_owned_root_pins(profile, source_uuids).exclude(pk=target.pk))
        if not sources:
            return HttpResponse("No valid source pins.", status=400)

        merged = 0
        for source in sources:
            # Structurally unreachable given the root-pins-only scoping above (a
            # root pin can never already be an ancestor of another root pin), but
            # kept as real defense-in-depth per the model's own guard contract.
            if source.would_create_cycle(target):
                continue
            source.parent_pin = target
            source.save(update_fields=["parent_pin"])
            merged += 1

        if not merged:
            return HttpResponse("Merge would create a cycle.", status=400)

        return JsonResponse({"ok": True, "merged": merged, "target_uuid": str(target.uuid)})


class PinBulkEditView(LoginRequiredMixin, View):
    """Bulk-edit description and badges across selected root pins (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        uuids = [str(x) for x in data.get("uuids", [])]
        if not uuids:
            return HttpResponse("No pins specified.", status=400)

        profile = _request_profile(request)
        pins = list(_owned_root_pins(profile, uuids))
        if not pins:
            return HttpResponse("No matching pins.", status=404)

        description = data.get("description")
        if description is not None and str(description).strip():
            length_error = text_length_error(description, MAX_PIN_DESCRIPTION_LENGTH, "Description")
            if length_error:
                return HttpResponse(length_error, status=400)
            for pin in pins:
                pin.description = description
                pin.save(update_fields=["description"])

        if add_ids := [int(x) for x in data.get("add_badge_ids", [])]:
            valid = list(Badge.objects.visible_to(profile).filter(id__in=add_ids, kind__in=_ORGANIZE_KINDS))
            for pin in pins:
                pin.badges.add(*valid)

        if remove_ids := [int(x) for x in data.get("remove_badge_ids", [])]:
            # Never trust the client's option list - only remove badges that are
            # actually present on at least one of the selected pins.
            removable = list(
                Badge.objects.filter(id__in=remove_ids, kind__in=_ORGANIZE_KINDS, pins__in=pins).distinct(),
            )
            for pin in pins:
                pin.badges.remove(*removable)

        return JsonResponse({"ok": True, "count": len(pins)})


class PinBulkEditBadgeOptionsView(LoginRequiredMixin, View):
    """Return the union of organize badges present on at least one of the given pins."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        uuids = request.GET.getlist("uuids")
        if not uuids:
            return JsonResponse({"badges": []})

        profile = _request_profile(request)
        pins = _owned_root_pins(profile, uuids)
        badges = Badge.objects.filter(kind__in=_ORGANIZE_KINDS, pins__in=pins).distinct().order_by("name")
        return JsonResponse(
            {
                "badges": [{"id": b.id, "name": b.name, "icon": b.effective_icon, "color": b.effective_color, "kind": b.kind} for b in badges],
            },
        )
