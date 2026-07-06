"""Markup views - lines, arrows, and text labels shared on a pin's or a location's map."""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location_edit import LocationEdit
from urbanlens.dashboard.models.markup.model import MarkupType, PinMarkup, SecurityIndicatorType
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = {mt.value for mt in MarkupType}
_ALLOWED_SECURITY_INDICATORS = {si.value for si in SecurityIndicatorType}

_INDICATOR_TO_FIELD: dict[str, str] = {
    "fence": "fences",
    "camera": "cameras",
    "alarm": "alarms",
    "security": "security",
    "sign": "signs",
    "plywood": "plywood",
    "locked": "locked",
    "vps": "vps",
}


def _apply_security_indicator(owner: Pin | Location, indicator: str) -> None:
    """Upgrade the matching security field on *owner* to at least 'some'.

    *owner* is either a Pin or a Location - both expose the same security
    fields via ``abstract.SecurityModel``. Only upgrades from unknown/no;
    never downgrades an existing value.
    """
    field = _INDICATOR_TO_FIELD.get(indicator)
    if not field:
        return
    current = getattr(owner, field, SecurityLevel.UNKNOWN)
    if current in {SecurityLevel.UNKNOWN, SecurityLevel.NO}:
        setattr(owner, field, SecurityLevel.SOME)
        owner.save(update_fields=[field])


_GEOMETRY_TYPES = {
    "line": "LineString",
    "arrow": "LineString",
    "text": "Point",
    "square": "Polygon",
    "circle": "Circle",  # Custom non-GeoJSON type stored as {"type":"Circle","coordinates":[lng,lat],"radius":m}
    "polygon": "Polygon",
}


def _sanitize_text_box_corner(geometry: dict) -> None:
    """Drop ``geometry["box_corner"]`` if it isn't a valid [lng, lat] pair.

    A drag-created text label stores the opposite corner of the box the user
    dragged out alongside its anchor point, so the frontend can size/wrap the
    label to fit it. Mutates *geometry* in place.
    """
    corner = geometry.get("box_corner")
    if corner is None:
        return
    valid = isinstance(corner, (list, tuple)) and len(corner) == 2 and all(isinstance(n, (int, float)) and math.isfinite(n) for n in corner)
    if not valid:
        geometry.pop("box_corner", None)


def _parse_body(request: HttpRequest) -> dict:
    """Parse JSON or fall back to POST data."""
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return dict(request.POST)


def _resolve_owner(
    request: HttpRequest,
    pin_slug: str | None,
    location_slug: str | None,
    safety_checkin_uuid: str | None = None,
) -> tuple[Pin | Location | SafetyCheckin, QuerySet[PinMarkup]]:
    """Resolve the markup owner (Pin, Location, or SafetyCheckin) from URL kwargs.

    Exactly one of *pin_slug* / *location_slug* / *safety_checkin_uuid* is
    expected to be set, matching the three URL patterns these views are
    mounted under - personal markup under a pin's own map, shared/community
    markup on a Location's wiki map, or a personal route/plan drawing on a
    safety check-in. Pin-scoped and check-in-scoped markup both require the
    caller to own the parent; Location-scoped markup is shared data any
    signed-in user may edit, matching the existing community detail-pin
    permission model.

    Args:
        request: The current HttpRequest (used for the ownership checks).
        pin_slug: Slug of the parent pin, if this is a personal-markup route.
        location_slug: Slug of the parent location, if this is a community-markup route.
        safety_checkin_uuid: UUID of the parent check-in, if this is a safety-checkin route.

    Returns:
        Tuple of (owner, markup queryset already filtered to that owner).
    """
    if pin_slug is not None:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return pin, PinMarkup.objects.for_pin(pin)
    if safety_checkin_uuid is not None:
        checkin = get_object_or_404(SafetyCheckin, uuid=safety_checkin_uuid, profile__user=request.user)
        return checkin, PinMarkup.objects.for_safety_checkin(checkin)
    location = get_object_or_404(Location, slug=location_slug)
    return location, PinMarkup.objects.for_location(location)


class MarkupJsonView(LoginRequiredMixin, View):
    """Return all markup items for a pin or location as JSON (for Leaflet rendering).

    GET /map/pin/<pin_slug>/markup/json/
    GET /location/<location_slug>/wiki/markup/json/
    """

    def get(self, request, pin_slug=None, location_slug=None, safety_checkin_uuid=None):
        """Return markup items as a JSON list.

        Args:
            request: HttpRequest.
            pin_slug: UUID/slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            safety_checkin_uuid: UUID of the parent check-in (safety-checkin route).

        Returns:
            JsonResponse with ``markup_items`` list.
        """
        _owner, items = _resolve_owner(request, pin_slug, location_slug, safety_checkin_uuid)
        return JsonResponse({"markup_items": [m.to_json() for m in items.order_by("created")]})


class MarkupView(LoginRequiredMixin, View):
    """Create a new markup item for a pin or location.

    POST /map/pin/<pin_slug>/markup/
    POST /location/<location_slug>/wiki/markup/
    """

    def post(self, request, pin_slug=None, location_slug=None, safety_checkin_uuid=None):
        """Create a markup item.

        Args:
            request: HttpRequest with JSON body containing markup fields.
            pin_slug: Slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            safety_checkin_uuid: UUID of the parent check-in (safety-checkin route).

        Returns:
            JsonResponse with ``ok`` and ``uuid`` on success, error on failure.
        """
        owner, _qs = _resolve_owner(request, pin_slug, location_slug, safety_checkin_uuid)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        body = _parse_body(request)

        markup_type = body.get("markup_type", "")
        if markup_type not in _ALLOWED_TYPES:
            return JsonResponse({"ok": False, "error": f"Invalid markup_type: {markup_type}"}, status=400)

        geometry = body.get("geometry")
        if not geometry or not isinstance(geometry, dict):
            return JsonResponse({"ok": False, "error": "geometry is required"}, status=400)

        expected_geom_type = _GEOMETRY_TYPES[markup_type]
        if geometry.get("type") != expected_geom_type:
            return JsonResponse(
                {"ok": False, "error": f"{markup_type} requires {expected_geom_type} geometry"},
                status=400,
            )
        if markup_type == "text":
            _sanitize_text_box_corner(geometry)

        label = (body.get("label") or "").strip()
        security_indicator = body.get("security_indicator") or ""
        if security_indicator not in _ALLOWED_SECURITY_INDICATORS:
            security_indicator = ""

        fill_opacity = int(body.get("fill_opacity") or profile.markup_fill_opacity)
        border_opacity = int(body.get("border_opacity") or profile.markup_border_opacity)

        if pin_slug is not None:
            owner_kwargs = {"parent_pin": owner}
        elif safety_checkin_uuid is not None:
            owner_kwargs = {"parent_safety_checkin": owner}
        else:
            owner_kwargs = {"parent_location": owner}
        item = PinMarkup.objects.create(
            profile=profile,
            markup_type=markup_type,
            geometry=geometry,
            label=label,
            color=body.get("color") or "#e53e3e",
            stroke_width=int(body.get("stroke_width") or 3),
            border_color=body.get("border_color") or "",
            fill_opacity=fill_opacity,
            border_opacity=border_opacity,
            security_indicator=security_indicator,
            **owner_kwargs,
        )
        if security_indicator and isinstance(owner, (Pin, Location)):
            _apply_security_indicator(owner, security_indicator)

        if location_slug is not None:
            LocationEdit.objects.create(
                location=owner,
                editor=profile,
                changes={"markup_added": {"from": None, "to": item.label or item.markup_type}},
            )
        return JsonResponse({"ok": True, "uuid": str(item.uuid)})


class MarkupEditView(LoginRequiredMixin, View):
    """Update or delete a single markup item.

    POST/DELETE /map/pin/<pin_slug>/markup/<markup_uuid>/
    POST/DELETE /location/<location_slug>/wiki/markup/<markup_uuid>/
    """

    def _get_item(self, request, pin_slug, location_slug, markup_uuid, safety_checkin_uuid=None) -> tuple[Pin | Location | SafetyCheckin, PinMarkup]:
        """Resolve a markup item, ensuring the caller may access its owner."""
        owner, qs = _resolve_owner(request, pin_slug, location_slug, safety_checkin_uuid)
        return owner, get_object_or_404(qs, uuid=markup_uuid)

    def post(self, request, pin_slug=None, location_slug=None, markup_uuid=None, safety_checkin_uuid=None):
        """Update mutable fields on a markup item.

        Args:
            request: HttpRequest with JSON body.
            pin_slug: Slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            markup_uuid: UUID of the markup item to update.
            safety_checkin_uuid: UUID of the parent check-in (safety-checkin route).

        Returns:
            JsonResponse with ``ok`` on success.
        """
        owner, item = self._get_item(request, pin_slug, location_slug, markup_uuid, safety_checkin_uuid)
        body = _parse_body(request)

        if "geometry" in body and isinstance(body["geometry"], dict):
            geometry = body["geometry"]
            if item.markup_type == "text":
                _sanitize_text_box_corner(geometry)
            item.geometry = geometry
        if "label" in body:
            item.label = (body["label"] or "").strip()
        if "color" in body:
            item.color = body["color"] or item.color
        if "stroke_width" in body:
            item.stroke_width = int(body["stroke_width"])
        if "border_color" in body:
            item.border_color = body["border_color"] or ""
        if "fill_opacity" in body:
            item.fill_opacity = int(body["fill_opacity"])
        if "border_opacity" in body:
            item.border_opacity = int(body["border_opacity"])
        if "security_indicator" in body:
            indicator = body.get("security_indicator") or ""
            item.security_indicator = indicator if indicator in _ALLOWED_SECURITY_INDICATORS else ""
        item.save()
        if item.security_indicator and isinstance(owner, (Pin, Location)):
            _apply_security_indicator(owner, item.security_indicator)
        return JsonResponse({"ok": True})

    def delete(self, request, pin_slug=None, location_slug=None, markup_uuid=None, safety_checkin_uuid=None):
        """Delete a markup item.

        Args:
            request: HttpRequest.
            pin_slug: Slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            markup_uuid: UUID of the markup item to delete.
            safety_checkin_uuid: UUID of the parent check-in (safety-checkin route).

        Returns:
            Empty 200 response on success.
        """
        owner, item = self._get_item(request, pin_slug, location_slug, markup_uuid, safety_checkin_uuid)
        label = item.label or item.markup_type
        item.delete()
        if location_slug is not None:
            profile, _ = Profile.objects.get_or_create(user=request.user)
            LocationEdit.objects.create(
                location=owner,
                editor=profile,
                changes={"markup_removed": {"from": label, "to": None}},
            )
        return HttpResponse("", status=200)
