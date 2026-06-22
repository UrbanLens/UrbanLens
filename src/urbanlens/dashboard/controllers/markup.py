"""Markup views - lines, arrows, and text labels on a pin's detail map."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.markup.model import MarkupType, PinMarkup, SecurityIndicatorType
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

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


def _apply_security_indicator(pin: Pin, indicator: str) -> None:
    """Upgrade the matching security field on *pin* to at least 'some'.

    Only upgrades from unknown/no; never downgrades an existing value.
    """
    field = _INDICATOR_TO_FIELD.get(indicator)
    if not field:
        return
    current = getattr(pin, field, SecurityLevel.UNKNOWN)
    if current in {SecurityLevel.UNKNOWN, SecurityLevel.NO}:
        setattr(pin, field, SecurityLevel.SOME)
        pin.save(update_fields=[field])


_GEOMETRY_TYPES = {
    "line": "LineString",
    "arrow": "LineString",
    "text": "Point",
    "square": "Polygon",
    "circle": "Circle",  # Custom non-GeoJSON type stored as {"type":"Circle","coordinates":[lng,lat],"radius":m}
    "polygon": "Polygon",
}


def _parse_body(request) -> dict:
    """Parse JSON or fall back to POST data."""
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return dict(request.POST)


class MarkupJsonView(LoginRequiredMixin, View):
    """Return all markup items for a pin as JSON (for Leaflet rendering).

    GET /map/pin/<pin_uuid>/markup/json/
    """

    def get(self, request, pin_uuid):
        """Return markup items as a JSON list.

        Args:
            request: HttpRequest.
            pin_uuid: UUID of the parent pin.

        Returns:
            JsonResponse with ``markup_items`` list.
        """
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        items = PinMarkup.objects.for_pin(pin).order_by("created")
        return JsonResponse({"markup_items": [m.to_json() for m in items]})


class MarkupView(LoginRequiredMixin, View):
    """Create a new markup item for a pin.

    POST /map/pin/<pin_uuid>/markup/
    """

    def post(self, request, pin_uuid):
        """Create a markup item.

        Args:
            request: HttpRequest with JSON body containing markup fields.
            pin_uuid: UUID of the parent pin.

        Returns:
            JsonResponse with ``ok`` and ``uuid`` on success, error on failure.
        """
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
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

        label = (body.get("label") or "").strip()
        security_indicator = body.get("security_indicator") or ""
        if security_indicator not in _ALLOWED_SECURITY_INDICATORS:
            security_indicator = ""

        fill_opacity = int(body.get("fill_opacity") or profile.markup_fill_opacity)
        border_opacity = int(body.get("border_opacity") or profile.markup_border_opacity)

        item = PinMarkup.objects.create(
            parent_pin=pin,
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
        )
        if security_indicator:
            _apply_security_indicator(pin, security_indicator)
        return JsonResponse({"ok": True, "uuid": str(item.uuid)})


class MarkupEditView(LoginRequiredMixin, View):
    """Update or delete a single markup item.

    POST /map/pin/<pin_uuid>/markup/<markup_uuid>/  - update geometry / label / color / width
    DELETE /map/pin/<pin_uuid>/markup/<markup_uuid>/  - delete
    """

    def _get_item(self, request, pin_uuid, markup_uuid) -> PinMarkup:
        """Resolve markup item ensuring the caller owns the parent pin."""
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        return get_object_or_404(PinMarkup, uuid=markup_uuid, parent_pin=pin)

    def post(self, request, pin_uuid, markup_uuid):
        """Update mutable fields on a markup item.

        Args:
            request: HttpRequest with JSON body.
            pin_uuid: UUID of the parent pin.
            markup_uuid: UUID of the markup item to update.

        Returns:
            JsonResponse with ``ok`` on success.
        """
        item = self._get_item(request, pin_uuid, markup_uuid)
        body = _parse_body(request)

        if "geometry" in body and isinstance(body["geometry"], dict):
            item.geometry = body["geometry"]
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
        if item.security_indicator:
            _apply_security_indicator(item.parent_pin, item.security_indicator)
        return JsonResponse({"ok": True})

    def delete(self, request, pin_uuid, markup_uuid):
        """Delete a markup item.

        Args:
            request: HttpRequest.
            pin_uuid: UUID of the parent pin.
            markup_uuid: UUID of the markup item to delete.

        Returns:
            Empty 200 response on success.
        """
        item = self._get_item(request, pin_uuid, markup_uuid)
        item.delete()
        return HttpResponse("", status=200)
