"""Detail-pin views - sub-markers placed within a pin's or location's bounding box."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType

logger = logging.getLogger(__name__)


class DetailPinPanelView(LoginRequiredMixin, View):
    """HTMX partial: list of detail pins for a single user pin."""

    def get(self, request, pin_uuid):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        detail_pins = (
            pin.detail_pins.select_related("location").prefetch_related("tags").order_by("pin_type", "nickname")
        )
        return render(
            request,
            "dashboard/partials/detail_pins_panel.html",
            {
                "pin": pin,
                "detail_pins": detail_pins,
                "pin_type_choices": PinType.choices,
            },
        )

    def post(self, request, pin_uuid):
        """Create a new detail pin under the given parent pin."""
        parent = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        lat = body.get("latitude")
        lon = body.get("longitude")
        if not lat or not lon:
            return JsonResponse({"ok": False, "error": "latitude and longitude required"}, status=400)

        detail_pin = Pin.objects.create(
            nickname=body.get("name") or None,
            description=body.get("description") or None,
            latitude=float(lat),
            longitude=float(lon),
            pin_type=body.get("pin_type") or PinType.POINT_OF_INTEREST,
            parent_pin=parent,
            profile=parent.profile,
            location=parent.location,
        )
        return JsonResponse({"ok": True, "uuid": str(detail_pin.uuid)})


class DetailPinEditView(LoginRequiredMixin, View):
    """Edit or delete a single detail pin."""

    def _get_detail_pin(self, request, pin_uuid, detail_pin_uuid):
        parent = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        return get_object_or_404(Pin, uuid=detail_pin_uuid, parent_pin=parent)

    def post(self, request, pin_uuid, detail_pin_uuid):
        """Update fields on a detail pin (used from HTMX form submit)."""
        detail_pin = self._get_detail_pin(request, pin_uuid, detail_pin_uuid)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        for field, value in {
            "nickname": body.get("name") or None,
            "description": body.get("description") or None,
            "pin_type": body.get("pin_type") or None,
        }.items():
            if value is not None or field in body:
                setattr(detail_pin, field, value)
        if body.get("latitude"):
            detail_pin.latitude = float(body["latitude"])
        if body.get("longitude"):
            detail_pin.longitude = float(body["longitude"])
        detail_pin.save()
        return JsonResponse({"ok": True})

    def delete(self, request, pin_uuid, detail_pin_uuid):
        detail_pin = self._get_detail_pin(request, pin_uuid, detail_pin_uuid)
        detail_pin.delete()
        return HttpResponse(status=204)


class DetailPinJsonView(LoginRequiredMixin, View):
    """Return detail pins as JSON for Leaflet rendering on the pin details page."""

    def get(self, request, pin_uuid):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        detail_pins = (
            pin.detail_pins.select_related("location").prefetch_related("tags").order_by("pin_type", "nickname")
        )
        return JsonResponse({"detail_pins": [dp.to_detail_json() for dp in detail_pins]})


class LocationDetailPinJsonView(LoginRequiredMixin, View):
    """Return all detail pins for a location (across all users) for the wiki map."""

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        detail_pins = (
            Pin.objects.filter(parent_pin__location=location)
            .select_related("parent_pin__profile__user")
            .prefetch_related("tags")
            .order_by("pin_type", "nickname")
        )
        data = []
        for dp in detail_pins:
            item = dp.to_detail_json()
            item["added_by"] = dp.parent_pin.profile.user.username if dp.parent_pin else "Unknown"
            data.append(item)
        return JsonResponse({"detail_pins": data})
