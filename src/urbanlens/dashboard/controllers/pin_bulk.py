"""Multi-select bulk actions for pins (root or child) on the main map: merge, delete+undo, bulk edit."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

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
from urbanlens.dashboard.models.reviews.model import Review
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


def _owned_pins(profile: Profile, uuids: list[str]) -> QuerySet[Pin]:
    """Pins (root or child) owned by ``profile`` among the given uuids.

    The main map's select tool can select both root and child (sub) pin
    markers, so bulk actions must be able to resolve either kind.
    """
    return Pin.objects.filter(profile=profile, uuid__in=uuids)


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

        subtree = list(Pin.objects.filter(pk__in=[p.pk for p in pins]).with_descendants())
        undo_action = stash_for_undo("pin", subtree, profile)
        for pin in subtree:
            pin.delete()

        descendant_count = len(subtree) - len(pins)
        return JsonResponse({"ok": True, "undo_token": str(undo_action.uuid), "count": len(pins), "descendant_count": descendant_count, "total_count": len(subtree)})


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
    """Merge selected pins: all but the target become the target's detail pins.

    The target becomes (or stays) the top-level pin; a target that's currently
    a child pin is promoted first (see the conflict check below).
    """

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
        target = get_object_or_404(Pin.objects.filter(profile=profile), uuid=target_uuid)
        if target.parent_pin_id is not None:
            # The target is itself a child pin - promote it to top-level first, since
            # merging always makes the chosen target the new top-level pin.
            conflict = Pin.objects.filter(profile=profile, location_id=target.location_id, parent_pin__isnull=True).exclude(pk=target.pk).exists()
            if conflict:
                return HttpResponse("You already have a top-level pin at this exact location. Choose a different pin as the merge target.", status=400)
            target.parent_pin = None
            target.save(update_fields=["parent_pin"])
        sources = list(_owned_pins(profile, source_uuids).exclude(pk=target.pk))
        if not sources:
            return HttpResponse("No valid source pins.", status=400)

        merged = 0
        for source in sources:
            # Structurally unreachable: by this point target is always root (either
            # already was, or was just promoted above), so it has no ancestors for
            # would_create_cycle to find a source in - kept as defense-in-depth per
            # the model's own guard contract, same as the original root-only version.
            if source.would_create_cycle(target):
                continue
            source.parent_pin = target
            source.save(update_fields=["parent_pin"])
            merged += 1

        if not merged:
            return HttpResponse("Merge would create a cycle.", status=400)

        return JsonResponse({"ok": True, "merged": merged, "target_uuid": str(target.uuid)})


class PinBulkEditView(LoginRequiredMixin, View):
    """Bulk-edit description, rating, labels, and parent pin across selected pins (JSON POST)."""

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        uuids = [str(x) for x in data.get("uuids", [])]
        if not uuids:
            return HttpResponse("No pins specified.", status=400)

        profile = _request_profile(request)
        pins = list(_owned_pins(profile, uuids))
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

        # rating lives on Review (one per profile/pin pair, see PinEditView.post
        # for the single-pin equivalent) - 0 explicitly clears every selected
        # pin's review; absent/invalid leaves ratings untouched.
        rating_raw = data.get("rating")
        if rating_raw is not None and str(rating_raw).strip():
            try:
                rating = int(rating_raw)
            except (TypeError, ValueError):
                rating = None
            if rating is not None and 1 <= rating <= 5:
                for pin in pins:
                    Review.objects.update_or_create(profile=profile, pin=pin, defaults={"rating": rating})
            elif rating == 0:
                Review.objects.filter(profile=profile, pin__in=pins).delete()

        if add_ids := [int(x) for x in data.get("add_label_ids", [])]:
            valid = list(Label.objects.visible_to(profile).filter(id__in=add_ids, kind__in=_ORGANIZE_KINDS))
            for pin in pins:
                pin.labels.add(*valid)

        if remove_ids := [int(x) for x in data.get("remove_label_ids", [])]:
            # Never trust the client's option list - only remove labels that are
            # actually present on at least one of the selected pins.
            removable = list(
                Label.objects.filter(id__in=remove_ids, kind__in=_ORGANIZE_KINDS, pins__in=pins).distinct(),
            )
            for pin in pins:
                pin.labels.remove(*removable)

        reparented = 0
        parent_uuid = str(data.get("parent_uuid") or "").strip()
        if parent_uuid:
            parent = get_object_or_404(Pin.objects.filter(profile=profile), uuid=parent_uuid)
            for pin in pins:
                if pin.pk == parent.pk or pin.would_create_cycle(parent):
                    continue
                pin.parent_pin = parent
                pin.save(update_fields=["parent_pin"])
                reparented += 1

        return JsonResponse({"ok": True, "count": len(pins), "reparented": reparented})


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
        exclude_uuids = set(request.GET.getlist("exclude"))
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

    A plain (non-JSON) form POST, not fetch/JSON like the other bulk views -
    submitted via a throwaway <form target="_blank"> so the browser handles
    the file download itself from the Content-Disposition header, with no
    URL-length limit on the pin count and no client-side blob handling.
    """

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from urbanlens.dashboard.services.export_formats import EXPORT_FORMATS

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
