"""Floorplan endpoints: fetch a building's plan, edit it, save it.

Floorplans are queried only through these routes - a building or pin fetched
anywhere else never drags plan data along (most buildings have none, and the
common case must stay free).

Routes hang off the pin, matching how the rest of a pin's map surface works
(markup, overlays): the pin resolves to its building place, and authorization
is pin ownership.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View
from django.views.generic import TemplateView

from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.place.model import Place

logger = logging.getLogger(__name__)


def _building_place(pin: Pin) -> Place | None:
    """The building place a pin's floorplan belongs to.

    A building pin resolves to its own structure; a parcel/location pin with
    exactly one building resolves to that. A multi-building parcel has no
    single plan - each building carries its own, so the editor offers a
    picker instead (see :func:`_building_choices`).
    """
    from urbanlens.dashboard.models.place.model import PlaceKind

    place = pin.location.place if (pin.location_id and pin.location.place_id) else None
    if place is None:
        return None
    if place.kind == PlaceKind.BUILDING:
        return place
    buildings = list(place.children.filter(kind=PlaceKind.BUILDING)[:2])
    return buildings[0] if len(buildings) == 1 else None


def _building_choices(pin: Pin) -> list[dict]:
    """The buildings on a pin's parcel that a floorplan could belong to.

    Empty unless the pin sits on a parcel holding several - the case where
    "this pin's floorplan" has no single answer. Each entry names the child
    pin covering that building when there is one, so the picker can send the
    user to the marker they already have rather than a bare place.
    """
    from urbanlens.dashboard.models.place.model import PlaceKind

    place = pin.location.place if (pin.location_id and pin.location.place_id) else None
    if place is None or place.kind == PlaceKind.BUILDING:
        return []
    buildings = list(place.children.filter(kind=PlaceKind.BUILDING).order_by("-area_sqm", "id")[:200])
    if len(buildings) < 2:
        return []

    children = {child.location.place_id: child for child in pin.detail_pins.select_related("location") if child.location_id and child.location.place_id}
    return [
        {
            "name": building.name or "Unnamed building",
            "pin_slug": children[building.pk].slug if building.pk in children else "",
            "area_sqm": building.area_sqm,
        }
        for building in buildings
    ]


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """Parse a ``min_lng,min_lat,max_lng,max_lat`` viewport filter.

    Raises:
        ValueError: Malformed, or with a minimum exceeding its maximum - which
            would silently match nothing rather than erroring.
    """
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise ValueError("bbox must be 'min_lng,min_lat,max_lng,max_lat'")
    try:
        min_lng, min_lat, max_lng, max_lat = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError("bbox values must all parse as numbers") from exc
    if min_lng > max_lng or min_lat > max_lat:
        raise ValueError("bbox minimums must not exceed its maximums")
    return (min_lng, min_lat, max_lng, max_lat)


def _parse_date(raw: str | None) -> datetime.date | None:
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


class FloorplanJsonView(LoginRequiredMixin, View):
    """GET /map/pin/<pin_slug>/floorplan/json/ - the plan document, resolved by date."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        """Return the pin's building floorplan as a document, or 204 when none.

        Args:
            request: May carry ``?date=YYYY-MM-DD`` to resolve a historical
                version.
            pin_slug: The pin whose building to resolve.

        Returns:
            JsonResponse with the document, or 204 - absence is normal, not
            an error.
        """
        from urbanlens.dashboard.services.floorplans.resolution import resolve_document

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        place = _building_place(pin)
        if place is None:
            return HttpResponse(status=204)
        version_uuid = request.GET.get("version") or ""
        if version_uuid:
            document = _document_for_version(place, pin.profile, version_uuid)
        else:
            document = resolve_document(place, profile=pin.profile, on_date=_parse_date(request.GET.get("date")))
        if document is None:
            return HttpResponse(status=204)
        # Every version of this building the user has, so the editor can offer
        # to switch between them without a second round trip.
        document["versions"] = _version_list(place, pin.profile)
        return JsonResponse(document)


def _version_list(place: Place, profile) -> list[dict]:
    """This user's plan versions for a building, oldest first."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.floorplans.features import bounds_of

    return [
        {
            "uuid": str(plan.uuid),
            "name": plan.name,
            "valid_from": plan.valid_from.isoformat() if plan.valid_from else None,
            # So a client can decide whether a version is worth drawing (or
            # where to fly to) without reading its document.
            "bounds": bounds_of(plan),
        }
        for plan in Floorplan.objects.for_place(place).filter(profile=profile)
    ]


def _document_for_version(place: Place, profile, version_uuid: str) -> dict | None:
    """One specific version of the user's own plan, or None."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.floorplans.serialization import document_for

    plan = Floorplan.objects.filter(place=place, profile=profile, uuid=version_uuid).first()
    return {**document_for(plan), "origin": "local"} if plan is not None else None


class FloorplanFeaturesView(LoginRequiredMixin, View):
    """GET /map/pin/<pin_slug>/floorplan/features/ - drawable items as GeoJSON.

    The map-facing counterpart to the document endpoint, for renderers and
    for any other software that speaks GeoJSON. Filters by viewport
    (``?bbox=min_lng,min_lat,max_lng,max_lat``), storey (``?level=``) and
    element kind (``?kind=``); ``?version=`` picks a specific plan version and
    ``?date=`` resolves one by date, exactly like the document endpoint.
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        """Return a GeoJSON FeatureCollection, or 204 when there is no plan.

        Returns:
            JsonResponse with the collection; 400 when a filter is malformed.
        """
        from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanElementKind
        from urbanlens.dashboard.services.floorplans.features import feature_collection

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        place = _building_place(pin)
        if place is None:
            return HttpResponse(status=204)

        version_uuid = request.GET.get("version") or ""
        if version_uuid:
            floorplan = Floorplan.objects.filter(place=place, profile=pin.profile, uuid=version_uuid).first()
        else:
            floorplan = Floorplan.objects.at(place, _parse_date(request.GET.get("date")), profile=pin.profile)
        if floorplan is None:
            return HttpResponse(status=204)

        try:
            bbox = _parse_bbox(request.GET.get("bbox"))
            level = int(request.GET["level"]) if request.GET.get("level") else None
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)

        kind = request.GET.get("kind", "")
        if kind and kind not in FloorplanElementKind.values:
            return JsonResponse({"ok": False, "error": f"kind must be one of {sorted(FloorplanElementKind.values)}"}, status=400)

        return JsonResponse(feature_collection(floorplan, bbox=bbox, level=level, kind=kind))


class FloorplanSaveView(LoginRequiredMixin, View):
    """POST /map/pin/<pin_slug>/floorplan/save/ - write the edited document."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        """Persist a full floorplan document for the pin's building.

        Args:
            request: JSON body holding the document; ``date`` inside it names
                the version being written.
            pin_slug: The pin whose building is being edited.

        Returns:
            JsonResponse with the saved document, or a 400 naming the defect.
        """
        from urbanlens.dashboard.services.floorplans.resolution import floorplan_for_editing
        from urbanlens.dashboard.services.floorplans.serialization import document_for, save_document

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        place = _building_place(pin)
        if place is None:
            return JsonResponse({"ok": False, "error": "This pin has no single building to attach a floorplan to."}, status=400)

        try:
            document = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
        if not isinstance(document, dict):
            return JsonResponse({"ok": False, "error": "Expected a document object."}, status=400)

        floorplan = floorplan_for_editing(
            place,
            pin.profile,
            version_uuid=str(document.get("uuid") or ""),
            on_date=_parse_date(document.get("valid_from")),
        )
        if floorplan.pin_id is None:
            floorplan.pin = pin
        try:
            save_document(floorplan, document, profile=pin.profile)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        saved = {**document_for(floorplan), "origin": "local", "versions": _version_list(place, pin.profile)}
        return JsonResponse({"ok": True, "floorplan": saved})


class FloorplanExtractView(LoginRequiredMixin, View):
    """POST /map/pin/<pin_slug>/floorplan/extract/<overlay_uuid>/ - trace a blueprint.

    Runs vision extraction over one of the pin's aligned blueprint overlays
    and returns suggested rooms/walls/openings in world coordinates. The
    editor merges them as editable suggestions; nothing is persisted here.
    """

    def post(self, request: HttpRequest, pin_slug: str, overlay_uuid) -> HttpResponse:
        """Extract structure from one overlay.

        Returns:
            JsonResponse with ``rooms``/``elements`` fragments; 422 when the
            sheet couldn't be read as a floorplan; 409 when AI isn't
            available for this install (the editor falls back to manual
            tracing and says so).
        """
        from urbanlens.dashboard.services.floorplans.extraction import extract_overlay_structure

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        overlay = get_object_or_404(pin.image_overlays.select_related("image"), uuid=overlay_uuid)

        structure = extract_overlay_structure(overlay)
        if structure is None:
            return JsonResponse({"ok": False, "error": "Automatic tracing isn't available - trace the sheet manually."}, status=409)
        if not structure.get("rooms") and not structure.get("elements"):
            return JsonResponse({"ok": False, "error": "Couldn't recognize a floorplan on this sheet."}, status=422)
        return JsonResponse({"ok": True, **structure})


class FloorplanPublishView(LoginRequiredMixin, View):
    """POST /map/pin/<pin_slug>/floorplan/publish/ - share a plan on the wiki."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        """Copy the named version onto the place's community wiki.

        Returns:
            JsonResponse with the published document, 404 when the version
            isn't the caller's, or 409 when the place has no community wiki.
        """
        from urbanlens.dashboard.models.floorplans.model import Floorplan
        from urbanlens.dashboard.services.floorplans.resolution import publish_to_wiki
        from urbanlens.dashboard.services.floorplans.serialization import document_for

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        place = _building_place(pin)
        if place is None:
            return JsonResponse({"ok": False, "error": "This pin has no single building."}, status=400)

        version_uuid = request.POST.get("version") or (json.loads(request.body or b"{}").get("uuid") if request.body else "")
        floorplan = Floorplan.objects.filter(place=place, uuid=version_uuid or None, profile=pin.profile, wiki__isnull=True).first()
        if floorplan is None:
            return JsonResponse({"ok": False, "error": "Save your floorplan before publishing it."}, status=404)

        community = publish_to_wiki(floorplan, pin.profile)
        if community is None:
            return JsonResponse({"ok": False, "error": "This place has no community wiki yet - create one from the pin page first."}, status=409)
        return JsonResponse({"ok": True, "floorplan": {**document_for(community), "origin": "community"}})


class FloorplanEditorView(LoginRequiredMixin, TemplateView):
    """GET /map/pin/<pin_slug>/floorplan/ - the visual editor page."""

    template_name = "dashboard/pages/floorplans/editor.html"

    def get_context_data(self, **kwargs):
        """Editor context: the pin, its building place, and its overlays.

        Returns:
            Context with ``pin``, ``place`` (may be None - the page explains),
            and the pin's georeferenced image overlays for tracing.
        """
        context = super().get_context_data(**kwargs)
        pin = get_object_or_404(
            Pin.objects.select_related("location", "profile"),
            slug=kwargs["pin_slug"],
            profile__user=self.request.user,
        )
        from urbanlens.dashboard.controllers.map_overlays import overlay_payload
        from urbanlens.dashboard.models.labels.model import Label

        context["pin"] = pin
        context["place"] = _building_place(pin)
        context["building_choices"] = _building_choices(pin) if context["place"] is None else []
        context["overlays_json"] = overlay_payload(pin.image_overlays.all())
        context["labels_json"] = [{"uuid": str(label.uuid), "name": label.name} for label in Label.objects.filter(profile=pin.profile).order_by("name")]
        return context
