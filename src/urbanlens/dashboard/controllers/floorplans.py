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
    single plan - each building child carries its own.
    """
    from urbanlens.dashboard.models.place.model import PlaceKind

    place = pin.location.place if (pin.location_id and pin.location.place_id) else None
    if place is None:
        return None
    if place.kind == PlaceKind.BUILDING:
        return place
    buildings = list(place.children.filter(kind=PlaceKind.BUILDING)[:2])
    return buildings[0] if len(buildings) == 1 else None


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
        document = resolve_document(place, on_date=_parse_date(request.GET.get("date")))
        if document is None:
            return HttpResponse(status=204)
        return JsonResponse(document)


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

        floorplan = floorplan_for_editing(place, pin.profile, on_date=_parse_date(document.get("valid_from")))
        if floorplan.pin_id is None:
            floorplan.pin = pin
        try:
            save_document(floorplan, document, profile=pin.profile)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "floorplan": {**document_for(floorplan), "origin": "local"}})


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
        context["pin"] = pin
        context["place"] = _building_place(pin)
        context["overlays"] = list(pin.image_overlays.select_related("image"))
        return context
