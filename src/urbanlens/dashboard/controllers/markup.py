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
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.meta import normalize_layer_mode
from urbanlens.dashboard.models.markup.model import CustomLayer, MarkupMap, MarkupType, PinMarkup, SecurityIndicatorType
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.core.colors import clean_color
from urbanlens.dashboard.services.core.numbers import safe_int
from urbanlens.dashboard.services.core.text_limits import MAX_MARKUP_LABEL_LENGTH, text_length_error
from urbanlens.dashboard.services.map.map_snapshot import default_markup_map_title, sanitize_map_data
from urbanlens.dashboard.services.sharing.map_sharing import clone_markup_map
from urbanlens.dashboard.services.undo.handlers.markup_map import MODEL_LABEL as MARKUP_MAP_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.visits.safety import notify_contacts_of_update
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to, resolve_visible_wiki

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
    # The real row, for the read as much as the write. A concealed projection
    # reports every indicator as UNKNOWN by rule, so reading one would turn this
    # never-downgrade rule into a downgrade - a place surveyed as EVERYWHERE
    # quietly reduced to SOME - and then the save would raise on the projection
    # anyway, as a 500 only concealed accounts receive.
    from urbanlens.dashboard.services.wiki.concealment import writable_wiki

    if isinstance(owner, Wiki):
        owner = writable_wiki(owner)
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
    the parent; Wiki-scoped markup is shared data any profile with a pin at
    that location may edit (see ``resolve_visible_wiki``), matching the
    existing community detail-pin permission model.

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
    if location_slug is None:
        raise Http404
    _location, wiki, profile = resolve_visible_wiki(request, location_slug)
    # Filtered by who drew it, not hidden outright. The reason to hide community
    # markup is that a hand-drawn entrance route says other people have been
    # inside and compared notes - and that reason does not cover the viewer's
    # own drawings, which tell them nothing they did not already know. Showing
    # someone their own work back is also the only option that does not announce
    # the concealment: a marker you placed yourself and cannot find afterwards
    # is a malfunction, and a malfunction only some accounts get is a tell.
    from urbanlens.dashboard.services.wiki.concealment import visible_rows

    return wiki, visible_rows(PinMarkup.objects.for_wiki(wiki), wiki, profile)


def _owner_layer_kwargs(owner: Pin | Wiki | MarkupMap) -> dict:
    """Return the CustomLayer filter kwargs (parent_pin/parent_wiki) for *owner*.

    A MarkupMap owner (standalone maps have no custom layers) yields kwargs
    that can never match any real CustomLayer, so a layer lookup against it
    always resolves to None rather than needing a special case at each call site.
    """
    if isinstance(owner, Pin):
        return {"parent_pin": owner}
    if isinstance(owner, Wiki):
        return {"parent_wiki": owner}
    return {"pk": None}


def _resolve_visible_layer(layer_uuid: str | None, owner: Pin | Wiki | MarkupMap, profile: Profile, owner_kwargs: dict) -> CustomLayer | None:
    """Resolve a posted ``layer_uuid`` to a layer this profile may actually assign an item to.

    Scoped by owner exactly as before (a layer belonging to a different pin/
    wiki, or any value on the map_uuid route, resolves to None); additionally
    scoped by ``visible_rows`` on a wiki owner, so a concealed viewer's own
    write can't file an item under a stranger's layer - the layer they'd have
    no way to see reflected back (the read side already nulls layer_uuid for
    exactly this case; refusing it here keeps the write side consistent with
    what the read side shows).

    Args:
        layer_uuid: The posted layer uuid, or a falsy value for "no layer".
        owner: The Pin, Wiki, or MarkupMap the item belongs to.
        profile: The requesting profile.
        owner_kwargs: The already-computed parent_pin/parent_wiki filter dict.

    Returns:
        The resolved CustomLayer, or None.
    """
    if not layer_uuid:
        return None
    qs = CustomLayer.objects.filter(uuid=layer_uuid, **owner_kwargs)
    if isinstance(owner, Wiki):
        from urbanlens.dashboard.services.wiki.concealment import visible_rows

        qs = visible_rows(qs, owner, profile)
    return qs.first()


def _current_layer_is_visible(layer_id: int, owner: Pin | Wiki | MarkupMap, profile: Profile) -> bool:
    """Whether *profile* could see the CustomLayer *layer_id* under *owner*.

    Always True off a wiki - concealment is a wiki-only concept, so a Pin's
    or MarkupMap's own layers are never filtered. Used only on the "clear
    this item's layer" write path, to tell a deliberate clear apart from a
    concealed viewer's edit-panel echoing back the None their own read side
    substituted for a layer they cannot see.
    """
    if not isinstance(owner, Wiki):
        return True
    from urbanlens.dashboard.services.wiki.concealment import visible_rows

    return visible_rows(CustomLayer.objects.filter(pk=layer_id), owner, profile).exists()


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
            zoom, layer_mode, show_borders, title) on the map route. On the
            pin route, ``?children=1`` additionally includes markup belonging
            to every descendant child pin, each item annotated with the owning
            child pin's name (``owner_name``).
        """
        owner, items = _resolve_owner(request, pin_slug, location_slug, map_uuid)
        include_children = pin_slug is not None and request.GET.get("children") == "1"
        if include_children and isinstance(owner, Pin):
            subtree = Pin.objects.filter(pk=owner.pk).with_descendants()
            items = PinMarkup.objects.filter(parent_pin__in=subtree).select_related("parent_pin__location", "parent_pin__location__wiki", "layer")

        # A visible item can still be filed under a layer this viewer cannot
        # see - wiki-scoped layer assignment isn't restricted to the item's
        # own author, so "my own drawing" and "the layer I put it in" are
        # independent visibility questions. Reusing the exact set
        # custom_layers._resolve_layer_owner would return keeps the two
        # surfaces from drifting: this is a no-op when concealment is off,
        # since visible_rows then returns every layer unfiltered.
        visible_layer_ids: set[int] | None = None
        if location_slug is not None and isinstance(owner, Wiki):
            from urbanlens.dashboard.services.wiki.concealment import visible_rows

            profile, _ = Profile.objects.get_or_create(user=request.user)
            visible_layer_ids = set(visible_rows(CustomLayer.objects.for_wiki(owner), owner, profile).values_list("pk", flat=True))

        markup_items = []
        for m in items.select_related("layer").order_by("created"):
            entry = m.to_json()
            if visible_layer_ids is not None and m.layer_id is not None and m.layer_id not in visible_layer_ids:
                # Ungroup rather than reference a layer this viewer cannot
                # list - the item itself stays visible (own/friend markup is
                # never hidden), it just renders as if filed under no layer.
                entry["layer_uuid"] = None
            if include_children and m.parent_pin_id is not None and m.parent_pin_id != owner.pk and m.parent_pin is not None:
                entry["owner_name"] = m.parent_pin.effective_name
            markup_items.append(entry)
        payload: dict = {"markup_items": markup_items}
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
        contact = get_object_or_404(SafetyCheckinContact.objects.select_related("checkin__markup_map").by_token(token))
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
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if location is None or not location_visible_to(location, profile):
            return None
        return Wiki.objects.filter(location=location).first()
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
        # When created from a specific pin's page (e.g. the pin-share dialog's
        # "New map" flow), associate the map with that pin immediately - see
        # MarkupMap.pin. _resolve_title_context() already scopes the Pin
        # lookup to the requesting profile.
        pin_for_map = context if isinstance(context, Pin) else None
        markup_map = MarkupMap.objects.create(profile=profile, title=default_markup_map_title(context), pin=pin_for_map)

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


class MarkupMapSnapshotView(LoginRequiredMixin, View):
    """Return a MarkupMap's full snapshot (viewport + markup) as JSON.

    Unlike ``MarkupJsonView`` (which returns each item's compact Leaflet-edit
    shape), this returns the same ``{center_lat, ..., markup: [{latlngs, ...}]}``
    format ``to_snapshot()`` embeds server-side for read-only thumbnails - the
    DM composer fetches it client-side to render a live preview of a map the
    caller is about to attach, before the message is sent.

    GET /markup-maps/<map_uuid>/snapshot/
    """

    def get(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Return the caller's own map as a snapshot dict.

        Args:
            request: HttpRequest.
            map_uuid: UUID of the map to read.

        Returns:
            JsonResponse with the snapshot fields, or 404 if the caller
            doesn't own that map.
        """
        markup_map = get_object_or_404(MarkupMap, uuid=map_uuid, profile__user=request.user)
        return JsonResponse(markup_map.to_snapshot())


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


class PinMarkupMapsView(LoginRequiredMixin, View):
    """ "Markup Maps" panel for the pin detail page (loaded via HTMX).

    Lists MarkupMaps directly associated with the pin (``MarkupMap.pin`` -
    e.g. created via the pin-share dialog's "New map" flow). Most pins have
    none, so this returns 204 in that case; the page's shared
    ``htmx:afterOnLoad`` handler removes the placeholder card entirely (same
    pattern as the external-data panels).

    GET /map/pin/<slug:pin_slug>/markup-maps/
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        """Render the panel, or 204 when the pin has no associated maps.

        Args:
            request: HttpRequest.
            pin_slug: Slug of the pin (scoped to the requesting profile).

        Returns:
            Rendered panel, or an empty 204 response.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        maps = list(MarkupMap.objects.filter(pin=pin).prefetch_related("items").order_by("-updated"))
        if not maps:
            return HttpResponse(status=204)
        map_cards = [{"map": markup_map, "snapshot": markup_map.to_snapshot(), "item_count": len(markup_map.items.all())} for markup_map in maps]
        return render(request, "dashboard/partials/pins/_pin_markup_maps_panel.html", {"pin": pin, "map_cards": map_cards})


class MarkupMapDeleteView(LoginRequiredMixin, View):
    """Delete a standalone MarkupMap (and, via cascade, its items).

    Host models reference maps with ``on_delete=SET_NULL``, so deleting a map
    that is still attached simply detaches it from its host - the host's own
    text/content is untouched. A ``pre_delete`` signal on ``MarkupMap`` (see
    ``models.markup.signals``) additionally flags every Comment/TripComment/
    DirectMessage referencing this map (``map_removed``), so those hosts can
    keep showing a "map removed" notice instead of silently losing all trace
    that one was ever attached.

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
        stash_for_undo(MARKUP_MAP_MODEL_LABEL, [markup_map], markup_map.profile)
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


def _map_visible_to(profile: Profile, markup_map: MarkupMap) -> Profile | None:
    """Return whoever sent ``markup_map`` to ``profile`` through a legitimate channel, if any.

    Checks the three ways a map can become visible to someone other than its
    owner: a DM attachment, a standalone :class:`MarkupMapShare`, or an
    attachment on an explicit :class:`PinShare`.

    Args:
        profile: The prospective recipient.
        markup_map: The map to check.

    Returns:
        The immediate sender if any channel grants ``profile`` access, else None.
    """
    dm = markup_map.direct_messages.filter(recipient=profile).select_related("sender").first()
    if dm is not None:
        return dm.sender
    map_share = markup_map.shares.filter(to_profile=profile).select_related("from_profile").first()
    if map_share is not None:
        return map_share.from_profile
    pin_share = markup_map.pin_share_attachments.filter(to_profile=profile).select_related("from_profile").first()
    if pin_share is not None:
        return pin_share.from_profile
    return None


class MarkupMapCloneView(LoginRequiredMixin, View):
    """ "Add to my maps": clone someone else's shared map into the caller's own maps.

    POST /markup-maps/<map_uuid>/clone/
    """

    def post(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Clone ``map_uuid`` into the caller's own Memories > Maps.

        Args:
            request: HttpRequest.
            map_uuid: UUID of the map to clone.

        Returns:
            Redirect to Memories > Maps on success, 400 if the caller already
            owns the map, or 404 if it was never shared with them.
        """
        recipient, _ = Profile.objects.get_or_create(user=request.user)
        source = get_object_or_404(MarkupMap, uuid=map_uuid)
        if source.profile_id == recipient.pk:
            return HttpResponse("This is already your own map.", status=400)
        sender = _map_visible_to(recipient, source)
        if sender is None:
            raise Http404
        existing = MarkupMap.objects.filter(profile=recipient, cloned_from=source).first()
        if existing is None:
            clone_markup_map(source, recipient, sender=sender)
        return redirect("memories.maps")


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

        fill_opacity = safe_int(body.get("fill_opacity"), profile.markup_fill_opacity)
        border_opacity = safe_int(body.get("border_opacity"), profile.markup_border_opacity)

        if pin_slug is not None:
            owner_kwargs = {"parent_pin": owner}
        elif map_uuid is not None:
            owner_kwargs = {"parent_map": owner}
        else:
            owner_kwargs = {"parent_wiki": owner}

        # CustomLayer only ever attaches to a Pin or Wiki (never a standalone
        # MarkupMap), so owner_kwargs' parent_pin/parent_wiki double as the
        # exact filter needed here - a layer_uuid belonging to a different
        # pin/wiki (or any value on the map_uuid route) silently resolves to
        # None rather than erroring, matching this view's existing lenient
        # validation style (see security_indicator above).
        layer = None
        if map_uuid is None:
            layer = _resolve_visible_layer(body.get("layer_uuid"), owner, profile, owner_kwargs)

        item = PinMarkup.objects.create(
            profile=profile,
            markup_type=markup_type,
            geometry=geometry,
            label=label,
            color=clean_color(body.get("color"), default="#e53e3e"),
            stroke_width=safe_int(body.get("stroke_width"), 3),
            border_color=clean_color(body.get("border_color"), default="", allow_none_keyword=True),
            fill_opacity=fill_opacity,
            border_opacity=border_opacity,
            security_indicator=security_indicator,
            layer=layer,
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
        profile, _ = Profile.objects.get_or_create(user=request.user)

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
            item.color = clean_color(body["color"], default=item.color)
        if "stroke_width" in body:
            item.stroke_width = safe_int(body["stroke_width"], item.stroke_width)
        if "border_color" in body:
            item.border_color = clean_color(body["border_color"], default="", allow_none_keyword=True)
        if "fill_opacity" in body:
            item.fill_opacity = safe_int(body["fill_opacity"], item.fill_opacity)
        if "border_opacity" in body:
            item.border_opacity = safe_int(body["border_opacity"], item.border_opacity)
        if "layer_uuid" in body:
            layer_uuid = body.get("layer_uuid")
            if layer_uuid:
                item.layer = _resolve_visible_layer(layer_uuid, owner, profile, _owner_layer_kwargs(owner))
            elif item.layer_id is None or _current_layer_is_visible(item.layer_id, owner, profile):
                # A genuine clear: nothing to clear, or the layer being
                # cleared was one this viewer could see and could
                # therefore have deliberately chosen to remove.
                item.layer = None
            # else: item.layer_id names a layer this viewer cannot see - a
            # concealed wiki hides that layer from the picker and nulls its
            # layer_uuid on read (MarkupJsonView.get), so an empty
            # layer_uuid here is indistinguishable from that display value
            # being echoed straight back by an edit to some other field.
            # Leaving the real assignment untouched is the only option that
            # doesn't destroy it - the same class of bug as the wiki
            # suggest-edit data loss fixed earlier this round (see
            # docs/PROBLEMS.md's "forms post every field" entry): a display
            # value must never be diffed/written back as the viewer's intent.
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
