"""Detail-pin views - sub-markers placed within a pin's or wiki's bounding box."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit

logger = logging.getLogger(__name__)


def _resolve_wiki(location_slug: str) -> tuple[Location, Wiki]:
    """Resolve the Location for a slug and its (lazily-created) Wiki."""
    location = get_object_or_404(Location, slug=location_slug)
    wiki, _created = Wiki.objects.get_or_create_for_location(location)
    return location, wiki


class DetailPinPanelView(LoginRequiredMixin, View):
    """HTMX partial: list of personal detail pins for a single user pin."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        detail_pins = pin.detail_pins.select_related("location").order_by("pin_type", "name")
        return render(
            request,
            "dashboard/partials/pins/detail_pins_panel.html",
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

        detail_name = body.get("name") or None
        detail_pin = Pin.objects.create(
            name=detail_name,
            name_is_user_provided=bool((detail_name or "").strip()),
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
        detail_pins = pin.detail_pins.order_by("pin_type", "name")
        return JsonResponse({"detail_pins": [dp.to_detail_json() for dp in detail_pins]})


class LocationDetailPinJsonView(LoginRequiredMixin, View):
    """Return community detail pins for a wiki (map overlay)."""

    def get(self, request, location_slug):
        _location, wiki = _resolve_wiki(location_slug)
        detail_pins = Pin.objects.filter(parent_wiki=wiki, parent_pin__isnull=True).select_related("profile__user").order_by("pin_type", "name")
        data = []
        viewer = request.user if request.user.is_authenticated else None
        for dp in detail_pins:
            item = dp.to_detail_json()
            if dp.profile and dp.profile.user:
                item["added_by"] = dp.profile.user.username
                item["is_mine"] = viewer is not None and dp.profile.user_id == viewer.pk
            else:
                item["added_by"] = "Unknown"
                item["is_mine"] = False
            data.append(item)
        return JsonResponse({"detail_pins": data})


class LocationWikiDetailPinView(LoginRequiredMixin, View):
    """Community detail pins for a wiki page.

    GET  → renders the (legacy, currently unused by the wiki page's own JS) panel partial.
    POST → creates a new community detail pin, records a WikiEdit, returns JSON.
    """

    def get(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        detail_pins = Pin.objects.filter(parent_wiki=wiki, parent_pin__isnull=True).select_related("profile__user").order_by("pin_type", "name")
        return render(
            request,
            "dashboard/partials/pins/location_detail_pins_panel.html",
            {
                "location": location,
                "wiki": wiki,
                "detail_pins": detail_pins,
                "pin_type_choices": PinType.choices,
            },
        )

    def post(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
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
            name_is_user_provided=bool((pin_name or "").strip()),
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
            parent_wiki=wiki,
            profile=profile,
            location=location,
        )

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"detail_pin_added": {"from": None, "to": detail_pin.effective_name}},
        )

        return JsonResponse({"ok": True, "uuid": str(detail_pin.uuid)})


class LocationWikiDetailPinEditView(LoginRequiredMixin, View):
    """Edit, move, or delete a community detail pin.

    Both verbs share one URL (mirroring the personal-pin equivalent,
    DetailPinEditView) so the frontend can use one base URL for both.
    Moves and deletes record a WikiEdit.
    """

    def post(self, request, location_slug, detail_pin_uuid):
        _location, wiki = _resolve_wiki(location_slug)
        detail_pin = get_object_or_404(Pin, uuid=detail_pin_uuid, parent_wiki=wiki)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        # Style/content fields update silently (no WikiEdit) - same reasoning
        # as personal detail pins: these autosave on every panel change, and a
        # granular audit entry per keystroke would flood the wiki's edit history.
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

        lat = body.get("latitude")
        lon = body.get("longitude")
        moved = bool(lat and lon)
        old_lat, old_lon = detail_pin.latitude, detail_pin.longitude
        if moved:
            detail_pin.latitude = float(lat)
            detail_pin.longitude = float(lon)
        detail_pin.save()

        if moved:
            WikiEdit.objects.create(
                wiki=wiki,
                editor=profile,
                changes={
                    "detail_pin_moved": {
                        "pin": detail_pin.effective_name,
                        "from": [str(old_lat), str(old_lon)],
                        "to": [str(detail_pin.latitude), str(detail_pin.longitude)],
                    }
                },
            )

        return JsonResponse({"ok": True})

    def delete(self, request, location_slug, detail_pin_uuid):
        _location, wiki = _resolve_wiki(location_slug)
        detail_pin = get_object_or_404(Pin, uuid=detail_pin_uuid, parent_wiki=wiki)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        pin_name = detail_pin.effective_name

        detail_pin.delete()

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"detail_pin_removed": {"from": pin_name, "to": None}},
        )

        return HttpResponse("", status=200)
