"""Markup views - annotation items on pin/wiki maps and standalone MarkupMaps.

Three parents can own markup items (see ``PinMarkup``): a Pin (personal
markup on the pin detail map), a Wiki (shared community markup), or a
standalone ``MarkupMap`` - the reusable container behind safety check-in
route maps, comment maps, and visit maps. The MarkupMap routes here also
cover creating draft maps (so a map can be drawn before its host object
exists, e.g. on the check-in creation page) and persisting the viewport.
"""

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
from urbanlens.dashboard.models.markup.meta import normalize_layer_mode
from urbanlens.dashboard.models.markup.model import MarkupMap, MarkupType, PinMarkup, SecurityIndicatorType
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.map_snapshot import default_markup_map_title, sanitize_map_data
from urbanlens.dashboard.services.safety import notify_contacts_of_update
from urbanlens.dashboard.services.text_limits import MAX_MARKUP_LABEL_LENGTH, text_length_error

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


def _apply_security_indicator(owner: Pin | Wiki, indicator: str) -> None:
    """Upgrade the matching security field on *owner* to at least 'some'.

    *owner* is either a Pin or a Wiki - both expose the same security
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


def _notify_linked_checkins(markup_map: MarkupMap, message: str) -> None:
    """Re-notify emergency contacts of check-ins whose route map just changed.

    Editing the route markup after contacts were already alerted is exactly
    the kind of plan change they need to hear about; ``notify_contacts_of_update``
    itself rate-limits and no-ops for non-escalated check-ins.

    Args:
        markup_map: The map that was edited.
        message: Short human-readable description of the change.
    """
    for checkin in SafetyCheckin.objects.filter(markup_map=markup_map):
        notify_contacts_of_update(checkin, message)


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
    map_uuid: str | None = None,
) -> tuple[Pin | Wiki | MarkupMap, QuerySet[PinMarkup]]:
    """Resolve the markup owner (Pin, Wiki, or MarkupMap) from URL kwargs.

    Exactly one of *pin_slug* / *location_slug* / *map_uuid* is expected to
    be set, matching the three URL patterns these views are mounted under -
    personal markup under a pin's own map, shared/community markup on a wiki
    map, or a standalone MarkupMap (safety check-in routes, comment/visit
    maps). Pin-scoped and map-scoped markup both require the caller to own
    the parent; Wiki-scoped markup is shared data any signed-in user may
    edit, matching the existing community detail-pin permission model. The
    community route is keyed by the Location slug and resolves
    (get-or-creates) that Location's Wiki.

    Args:
        request: The current HttpRequest (used for the ownership checks).
        pin_slug: Slug of the parent pin, if this is a personal-markup route.
        location_slug: Slug of the parent location, if this is a community-markup route.
        map_uuid: UUID of the parent MarkupMap, if this is a standalone-map route.

    Returns:
        Tuple of (owner, markup queryset already filtered to that owner).
    """
    if pin_slug is not None:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return pin, PinMarkup.objects.for_pin(pin)
    if map_uuid is not None:
        markup_map = get_object_or_404(MarkupMap, uuid=map_uuid, profile__user=request.user)
        return markup_map, PinMarkup.objects.for_map(markup_map)
    location = get_object_or_404(Location, slug=location_slug)
    wiki = get_object_or_404(Wiki, location=location)
    return wiki, PinMarkup.objects.for_wiki(wiki)


class MarkupJsonView(LoginRequiredMixin, View):
    """Return all markup items for a pin, location, or markup map as JSON.

    GET /map/pin/<pin_slug>/markup/json/
    GET /location/<location_slug>/wiki/markup/json/
    GET /markup-maps/<map_uuid>/json/
    """

    def get(self, request, pin_slug=None, location_slug=None, map_uuid=None):
        """Return markup items (and, for maps, the saved viewport) as JSON.

        Args:
            request: HttpRequest.
            pin_slug: UUID/slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            map_uuid: UUID of the parent MarkupMap (standalone-map route).

        Returns:
            JsonResponse with ``markup_items`` list, plus ``view`` (centre,
            zoom, layer_mode, show_borders, title) on the map route.
        """
        owner, items = _resolve_owner(request, pin_slug, location_slug, map_uuid)
        payload: dict = {"markup_items": [m.to_json() for m in items.order_by("created")]}
        if isinstance(owner, MarkupMap):
            payload["view"] = {
                "center_lat": owner.center_latitude,
                "center_lng": owner.center_longitude,
                "zoom": owner.zoom,
                "layer_mode": owner.layer_mode,
                "show_borders": owner.show_borders,
                "title": owner.title,
            }
        return JsonResponse(payload)


class SafetyContactMarkupJsonView(View):
    """Read-only markup JSON for the public, token-gated safety contact portal.

    Deliberately not ``LoginRequiredMixin`` - an emergency contact has no
    account to log into, only the magic-link ``token`` mailed to them, so
    this mirrors the token-based auth already used by
    ``SafetyContactPortalView``/``SafetyContactMarkSafeView`` instead of the
    owner-only ``MarkupJsonView``.

    GET /safety/contact/<uuid:token>/markup/json/
    """

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        """Return the linked check-in's route-map markup items as a JSON list.

        Args:
            request: HttpRequest.
            token: The contact's magic-link token.

        Returns:
            JsonResponse with ``markup_items`` list, or 404 if the token is invalid.
        """
        contact = get_object_or_404(SafetyCheckinContact.objects.select_related("checkin__markup_map"), token=token)
        markup_map = contact.checkin.markup_map
        if markup_map is None:
            return JsonResponse({"markup_items": []})
        items = PinMarkup.objects.for_map(markup_map)
        return JsonResponse({"markup_items": [m.to_json() for m in items.order_by("created")]})


def _resolve_title_context(request: HttpRequest, body: dict) -> Pin | Wiki | None:
    """Resolve the optional Pin/Wiki a standalone-map creation is scoped to.

    Lets the "take a screenshot" toolbar buttons on the pin detail and wiki
    pages tell the server which pin/wiki they were opened from, purely for
    ``default_markup_map_title()`` purposes - unlike the personal/community
    markup routes, ownership is never enforced against this (a new MarkupMap
    is always its own thing, owned by the caller).

    Args:
        request: HttpRequest (used to scope the pin lookup to its owner).
        body: Parsed request body, optionally carrying ``pin_slug`` or
            ``location_slug``.

    Returns:
        The matching Pin or Wiki, or None when neither slug was given/found.
    """
    pin_slug = body.get("pin_slug")
    if pin_slug:
        return Pin.objects.filter(slug=pin_slug, profile__user=request.user).first()
    location_slug = body.get("location_slug")
    if location_slug:
        location = Location.objects.filter(slug=location_slug).first()
        return Wiki.objects.filter(location=location).first() if location else None
    return None


class MarkupMapCreateView(LoginRequiredMixin, View):
    """Create a new standalone MarkupMap - either a draft, or a fully-drawn one.

    Used two ways:

    - As a lazy draft, by pages that let the user draw a map before its host
      object exists (e.g. the safety check-in creation page) - no ``markup``/
      ``shapes`` key is sent, so only the initial viewport is applied.
    - As a one-shot save, by the shared map composer's standalone mode (the
      "take a screenshot" toolbar buttons) - a ``markup`` (or ``shapes``) list
      is sent alongside the viewport, so the map is fully populated and
      immediately browsable (e.g. from Memories > Maps) without needing a
      host object to attach to at all.

    POST /markup-maps/new/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        """Create a MarkupMap owned by the caller, optionally fully populated.

        Accepts optional JSON body fields ``center_lat``/``center_lng``/
        ``zoom``/``layer_mode``/``show_borders``/``title`` for the initial
        viewport, plus ``pin_slug``/``location_slug`` (used only to pick a
        sensible default title) and ``markup``/``shapes`` (a full snapshot's
        markup list, which switches this into the one-shot save mode).

        Args:
            request: HttpRequest.

        Returns:
            JsonResponse with ``ok`` and the new map's ``uuid``, or a 400 with
            ``ok: False`` when a submitted snapshot fails validation.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        body = _parse_body(request)
        context = _resolve_title_context(request, body)
        markup_map = MarkupMap.objects.create(profile=profile, title=default_markup_map_title(context))

        if isinstance(body.get("markup"), list) or isinstance(body.get("shapes"), list):
            snapshot = sanitize_map_data(body)
            if snapshot is None:
                return JsonResponse({"ok": False, "error": "Invalid map data"}, status=400)
            explicit_title = str(body.get("title") or "").strip()[:200]
            if explicit_title:
                markup_map.title = explicit_title
            markup_map.replace_items_from_snapshot(snapshot)
        else:
            _apply_view_state(markup_map, body)

        return JsonResponse({"ok": True, "uuid": str(markup_map.uuid)})


def _apply_view_state(markup_map: MarkupMap, body: dict) -> None:
    """Apply viewport fields from a request body onto *markup_map* and save.

    Ignores fields that are absent or invalid; clamps zoom to Leaflet's range.

    Args:
        markup_map: The map to update.
        body: Parsed request body.
    """
    updates: list[str] = []
    for body_key, field in (("center_lat", "center_latitude"), ("center_lng", "center_longitude"), ("zoom", "zoom")):
        value = body.get(body_key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            setattr(markup_map, field, float(value))
            updates.append(field)
    if markup_map.zoom is not None:
        markup_map.zoom = max(1.0, min(22.0, markup_map.zoom))
    # Accepts canonical values plus legacy aliases from older cached clients;
    # anything unrecognized is ignored rather than coerced.
    layer_mode = normalize_layer_mode(body.get("layer_mode"), default=None)
    if layer_mode is not None:
        markup_map.layer_mode = layer_mode
        updates.append("layer_mode")
    if "show_borders" in body:
        markup_map.show_borders = bool(body.get("show_borders"))
        updates.append("show_borders")
    if "title" in body:
        markup_map.title = str(body.get("title") or "")[:200]
        updates.append("title")
    if updates:
        markup_map.save(update_fields=[*updates, "updated"])


class MarkupMapViewStateView(LoginRequiredMixin, View):
    """Persist a MarkupMap's viewport (centre/zoom/layer/borders) and title.

    Autosaved by the map widget on move/zoom/layer changes, so a re-opened
    map restores exactly how the user left it.

    POST /markup-maps/<map_uuid>/view/
    """

    def post(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Update viewport fields from the JSON body.

        Args:
            request: HttpRequest with JSON body.
            map_uuid: UUID of the map to update.

        Returns:
            JsonResponse with ``ok``.
        """
        markup_map = get_object_or_404(MarkupMap, uuid=map_uuid, profile__user=request.user)
        _apply_view_state(markup_map, _parse_body(request))
        return JsonResponse({"ok": True})


class MarkupMapDeleteView(LoginRequiredMixin, View):
    """Delete a standalone MarkupMap (and, via cascade, its items).

    Host models reference maps with ``on_delete=SET_NULL``, so deleting a map
    that is still attached simply detaches it from its host.

    POST/DELETE /markup-maps/<map_uuid>/delete/
    """

    def post(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Delete the map.

        Args:
            request: HttpRequest.
            map_uuid: UUID of the map to delete.

        Returns:
            Empty 200 response on success.
        """
        markup_map = get_object_or_404(MarkupMap, uuid=map_uuid, profile__user=request.user)
        markup_map.delete()
        return HttpResponse("", status=200)

    def delete(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Delete the map (DELETE verb alias for :meth:`post`).

        Args:
            request: HttpRequest.
            map_uuid: UUID of the map to delete.

        Returns:
            Empty 200 response on success.
        """
        return self.post(request, map_uuid)


class MarkupView(LoginRequiredMixin, View):
    """Create a new markup item for a pin, location, or markup map.

    POST /map/pin/<pin_slug>/markup/
    POST /location/<location_slug>/wiki/markup/
    POST /markup-maps/<map_uuid>/markup/
    """

    def post(self, request, pin_slug=None, location_slug=None, map_uuid=None):
        """Create a markup item.

        Args:
            request: HttpRequest with JSON body containing markup fields.
            pin_slug: Slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            map_uuid: UUID of the parent MarkupMap (standalone-map route).

        Returns:
            JsonResponse with ``ok`` and ``uuid`` on success, error on failure.
        """
        owner, _qs = _resolve_owner(request, pin_slug, location_slug, map_uuid)
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
        length_error = text_length_error(label, MAX_MARKUP_LABEL_LENGTH, "Label")
        if length_error:
            return JsonResponse({"ok": False, "error": length_error}, status=400)
        security_indicator = body.get("security_indicator") or ""
        if security_indicator not in _ALLOWED_SECURITY_INDICATORS:
            security_indicator = ""

        fill_opacity = int(body.get("fill_opacity") or profile.markup_fill_opacity)
        border_opacity = int(body.get("border_opacity") or profile.markup_border_opacity)

        if pin_slug is not None:
            owner_kwargs = {"parent_pin": owner}
        elif map_uuid is not None:
            owner_kwargs = {"parent_map": owner}
        else:
            owner_kwargs = {"parent_wiki": owner}
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
        if security_indicator and isinstance(owner, (Pin, Wiki)):
            _apply_security_indicator(owner, security_indicator)

        if location_slug is not None:
            WikiEdit.objects.create(
                wiki=owner,
                editor=profile,
                changes={"markup_added": {"from": None, "to": item.label or item.markup_type}},
            )
        if isinstance(owner, MarkupMap):
            _notify_linked_checkins(owner, "added an annotation to the route map")
        return JsonResponse({"ok": True, "uuid": str(item.uuid)})


class MarkupEditView(LoginRequiredMixin, View):
    """Update or delete a single markup item.

    POST/DELETE /map/pin/<pin_slug>/markup/<markup_uuid>/
    POST/DELETE /location/<location_slug>/wiki/markup/<markup_uuid>/
    POST/DELETE /markup-maps/<map_uuid>/markup/<markup_uuid>/
    """

    def _get_item(self, request, pin_slug, location_slug, markup_uuid, map_uuid=None) -> tuple[Pin | Wiki | MarkupMap, PinMarkup]:
        """Resolve a markup item, ensuring the caller may access its owner."""
        owner, qs = _resolve_owner(request, pin_slug, location_slug, map_uuid)
        return owner, get_object_or_404(qs, uuid=markup_uuid)

    def post(self, request, pin_slug=None, location_slug=None, markup_uuid=None, map_uuid=None):
        """Update mutable fields on a markup item.

        Args:
            request: HttpRequest with JSON body.
            pin_slug: Slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            markup_uuid: UUID of the markup item to update.
            map_uuid: UUID of the parent MarkupMap (standalone-map route).

        Returns:
            JsonResponse with ``ok`` on success.
        """
        owner, item = self._get_item(request, pin_slug, location_slug, markup_uuid, map_uuid)
        body = _parse_body(request)

        if "geometry" in body and isinstance(body["geometry"], dict):
            geometry = body["geometry"]
            if item.markup_type == "text":
                _sanitize_text_box_corner(geometry)
            item.geometry = geometry
        if "label" in body:
            label = (body["label"] or "").strip()
            length_error = text_length_error(label, MAX_MARKUP_LABEL_LENGTH, "Label")
            if length_error:
                return JsonResponse({"ok": False, "error": length_error}, status=400)
            item.label = label
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
        if item.security_indicator and isinstance(owner, (Pin, Wiki)):
            _apply_security_indicator(owner, item.security_indicator)
        if isinstance(owner, MarkupMap):
            _notify_linked_checkins(owner, "updated an annotation on the route map")
        return JsonResponse({"ok": True})

    def delete(self, request, pin_slug=None, location_slug=None, markup_uuid=None, map_uuid=None):
        """Delete a markup item.

        Args:
            request: HttpRequest.
            pin_slug: Slug of the parent pin (personal markup route).
            location_slug: Slug of the parent location (community markup route).
            markup_uuid: UUID of the markup item to delete.
            map_uuid: UUID of the parent MarkupMap (standalone-map route).

        Returns:
            Empty 200 response on success.
        """
        owner, item = self._get_item(request, pin_slug, location_slug, markup_uuid, map_uuid)
        label = item.label or item.markup_type
        item.delete()
        if location_slug is not None:
            profile, _ = Profile.objects.get_or_create(user=request.user)
            WikiEdit.objects.create(
                wiki=owner,
                editor=profile,
                changes={"markup_removed": {"from": label, "to": None}},
            )
        if isinstance(owner, MarkupMap):
            _notify_linked_checkins(owner, "removed an annotation from the route map")
        return HttpResponse("", status=200)
