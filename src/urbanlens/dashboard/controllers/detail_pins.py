"""Detail-pin views - sub-markers placed within a pin's or location's bounding box."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location_edit import LocationEdit
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class DetailPinPanelView(LoginRequiredMixin, View):
    """HTMX partial: list of personal detail pins for a single user pin."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        detail_pins = (
            pin.detail_pins.select_related("location").order_by("pin_type", "name")
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

    def post(self, request, pin_slug):
        """Create a new personal detail pin under the given parent pin."""
        parent = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        lat = body.get("latitude")
        lon = body.get("longitude")
        if not lat or not lon:
            return JsonResponse({"ok": False, "error": "latitude and longitude required"}, status=400)

        detail_pin = Pin.objects.create(
            name=body.get("name") or None,
            description=body.get("description") or None,
            latitude=float(lat),
            longitude=float(lon),
            pin_type=body.get("pin_type") or PinType.POINT_OF_INTEREST,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
            detail_bg_color=body.get("bg_color") or None,
            detail_bg_opacity=int(body.get("bg_opacity") or 80),
            detail_border_color=body.get("border_color") or None,
            detail_border_opacity=int(body.get("border_opacity") or 100),
            parent_pin=parent,
            profile=parent.profile,
            location=parent.location,
        )
        return JsonResponse({"ok": True, "uuid": str(detail_pin.uuid)})


class DetailPinEditView(LoginRequiredMixin, View):
    """Edit or delete a single personal detail pin."""

    def _get_detail_pin(self, request, pin_slug, detail_pin_uuid):
        parent = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return get_object_or_404(Pin, uuid=detail_pin_uuid, parent_pin=parent)

    def post(self, request, pin_slug, detail_pin_uuid):
        detail_pin = self._get_detail_pin(request, pin_slug, detail_pin_uuid)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        for field, value in {
            "name": body.get("name") or None,
            "description": body.get("description") or None,
            "pin_type": body.get("pin_type") or None,
            "icon": body.get("icon") or None,
            "color": body.get("color") or None,
            "detail_bg_color": body.get("bg_color") or None,
            "detail_border_color": body.get("border_color") or None,
        }.items():
            if value is not None or field in body:
                setattr(detail_pin, field, value)
        if "bg_opacity" in body:
            detail_pin.detail_bg_opacity = int(body["bg_opacity"])
        if "border_opacity" in body:
            detail_pin.detail_border_opacity = int(body["border_opacity"])
        if body.get("latitude"):
            detail_pin.latitude = float(body["latitude"])
        if body.get("longitude"):
            detail_pin.longitude = float(body["longitude"])
        detail_pin.save()
        return JsonResponse({"ok": True})

    def delete(self, request, pin_slug, detail_pin_uuid):
        detail_pin = self._get_detail_pin(request, pin_slug, detail_pin_uuid)
        detail_pin.delete()
        return HttpResponse("", status=200)


class DetailPinJsonView(LoginRequiredMixin, View):
    """Return personal detail pins as JSON for Leaflet rendering on the pin details page."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        detail_pins = (
            pin.detail_pins.order_by("pin_type", "name")
        )
        return JsonResponse({"detail_pins": [dp.to_detail_json() for dp in detail_pins]})


class LocationDetailPinJsonView(LoginRequiredMixin, View):
    """Return community detail pins for a location (wiki map overlay)."""

    def get(self, request, location_slug):
        location = get_object_or_404(Location, slug=location_slug)
        detail_pins = (
            Pin.objects.filter(parent_location=location, parent_pin__isnull=True)
            .select_related("profile__user")
            .order_by("pin_type", "name")
        )
        data = []
        for dp in detail_pins:
            item = dp.to_detail_json()
            item["added_by"] = dp.profile.user.username if dp.profile else "Unknown"
            data.append(item)
        return JsonResponse({"detail_pins": data})


class LocationWikiDetailPinView(LoginRequiredMixin, View):
    """HTMX partial: community detail pins panel for a Location wiki page.

    GET  → renders the panel partial.
    POST → creates a new community detail pin, records a LocationEdit, re-renders.
    """

    def _render(self, request, location):
        detail_pins = (
            Pin.objects.filter(parent_location=location, parent_pin__isnull=True)
            .select_related("profile__user")
            .order_by("pin_type", "name")
        )
        return render(
            request,
            "dashboard/partials/location_detail_pins_panel.html",
            {
                "location": location,
                "detail_pins": detail_pins,
                "pin_type_choices": PinType.choices,
            },
        )

    def get(self, request, location_slug):
        location = get_object_or_404(Location, slug=location_slug)
        return self._render(request, location)

    def post(self, request, location_slug):
        location = get_object_or_404(Location, slug=location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        lat = body.get("latitude")
        lon = body.get("longitude")
        if not lat or not lon:
            return JsonResponse({"ok": False, "error": "latitude and longitude required"}, status=400)

        pin_name = body.get("name") or None
        detail_pin = Pin.objects.create(
            name=pin_name,
            description=body.get("description") or None,
            latitude=float(lat),
            longitude=float(lon),
            pin_type=body.get("pin_type") or PinType.POINT_OF_INTEREST,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
            parent_location=location,
            profile=profile,
            location=location,
        )

        LocationEdit.objects.create(
            location=location,
            editor=profile,
            changes={"detail_pin_added": {"from": None, "to": detail_pin.effective_name}},
        )

        return self._render(request, location)


class LocationWikiDetailPinEditView(LoginRequiredMixin, View):
    """Move a community detail pin and record a LocationEdit."""

    def post(self, request, location_slug, detail_pin_uuid):
        location = get_object_or_404(Location, slug=location_slug)
        detail_pin = get_object_or_404(Pin, uuid=detail_pin_uuid, parent_location=location)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        lat = body.get("latitude")
        lon = body.get("longitude")
        if not lat or not lon:
            return JsonResponse({"ok": False, "error": "latitude and longitude required"}, status=400)

        old_lat = detail_pin.latitude
        old_lon = detail_pin.longitude
        detail_pin.latitude = float(lat)
        detail_pin.longitude = float(lon)
        detail_pin.save()

        LocationEdit.objects.create(
            location=location,
            editor=profile,
            changes={"detail_pin_moved": {
                "pin": detail_pin.effective_name,
                "from": [str(old_lat), str(old_lon)],
                "to": [str(detail_pin.latitude), str(detail_pin.longitude)],
            }},
        )

        return JsonResponse({"ok": True})


class LocationWikiDetailPinDeleteView(LoginRequiredMixin, View):
    """Delete a single community detail pin and record a LocationEdit."""

    def delete(self, request, location_slug, detail_pin_uuid):
        location = get_object_or_404(Location, slug=location_slug)
        detail_pin = get_object_or_404(Pin, uuid=detail_pin_uuid, parent_location=location)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        pin_name = detail_pin.effective_name

        detail_pin.delete()

        LocationEdit.objects.create(
            location=location,
            editor=profile,
            changes={"detail_pin_removed": {"from": pin_name, "to": None}},
        )

        detail_pins = (
            Pin.objects.filter(parent_location=location, parent_pin__isnull=True)
            .select_related("profile__user")
            .order_by("pin_type", "name")
        )
        return render(
            request,
            "dashboard/partials/location_detail_pins_panel.html",
            {
                "location": location,
                "detail_pins": detail_pins,
                "pin_type_choices": PinType.choices,
            },
        )
