"""Detail-pin views - sub-markers placed within a pin's or wiki's bounding box."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.locations.site_scope import is_site_scope
from urbanlens.dashboard.services.undo.handlers.wiki import with_wiki_descendants
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.wiki_access import location_visible_to, resolve_visible_wiki

logger = logging.getLogger(__name__)

#: Type a marker gets while automatic classification hasn't run (or found no
#: building). Point of Interest is the honest provisional answer - most
#: hand-placed sub-markers really are landmarks rather than structures, and
#: it was this dialog's effective default before "Auto" existed.
_PROVISIONAL_PIN_TYPE = PinType.POINT_OF_INTEREST


def _requested_pin_type(body) -> tuple[str, bool]:
    """Resolve a submitted ``pin_type`` into a value plus "the user chose it".

    The dialog's Type select offers "Auto" as a blank value, so a blank
    submission means "work it out for me" rather than "no opinion recorded" -
    the distinction ``pin_type_is_user_provided`` exists to keep (see
    ``services.locations.site_scope.classify_building_pin_type``).

    Args:
        body: The parsed request body (JSON dict or QueryDict).

    Returns:
        Tuple of (pin type to store now, whether the user picked it).
    """
    chosen = (body.get("pin_type") or "").strip()
    if chosen and PinType.valid(chosen):
        return chosen, True
    return _PROVISIONAL_PIN_TYPE, False


def _schedule_classification(kind: str, pk: int) -> None:
    """Queue automatic building classification for a newly placed marker."""
    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import classify_detail_marker

    safely_enqueue_task(classify_detail_marker, kind, pk)


def _location_for_coords(latitude, longitude) -> Location:
    """Find-or-create the Location a detail pin sits at.

    A detail pin has its own coordinates (distinct from its parent's), and a Pin
    reads its coordinates from its Location, so each detail pin needs its own
    Location row at its point. ``get_nearby_or_create``'s default 50m proximity
    dedup would otherwise snap two detail pins placed within 50m of each other
    (or of the parent pin itself) onto the same Location, collapsing their
    coordinates together - so this skips that dedup and only reuses an existing
    Location on an exact coordinate match, mirroring ``_location_for_child_wiki``.
    """
    location, _created = Location.objects.get_nearby_or_create(float(latitude), float(longitude), threshold_meters=0)
    return location


def _location_for_child_wiki(latitude, longitude) -> Location:
    """Find-or-create a Location for a new child wiki's own coordinates.

    A Wiki's ``location`` is one-to-one, so - unlike a detail pin's plain FK -
    a child wiki can never share a Location with any other Wiki. The usual
    proximity-based dedup (``Location.objects.get_nearby_or_create``'s default
    50m threshold) would otherwise merge two nearby child markers, or a
    marker and its own parent, onto the same Location and collide. This skips
    that dedup and only reuses an existing Location when it's an exact
    coordinate match that has no wiki of its own yet.
    """
    latitude, longitude = float(latitude), float(longitude)
    location, created = Location.objects.get_nearby_or_create(latitude, longitude, threshold_meters=0)
    if created:
        return location
    try:
        _existing_wiki = location.wiki
    except ObjectDoesNotExist:
        return location
    return Location.objects.create(latitude=latitude, longitude=longitude)


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
                "is_site_scope": is_site_scope(pin),
                "has_wiki": Wiki.objects.get_for_location(pin.location) is not None,
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

        # Defense-in-depth: this endpoint only ever creates a brand-new child pin
        # (never re-parents an existing one), so a cycle can't actually form here
        # today. Guard anyway so this stays safe if that ever changes, and so a
        # pre-existing corrupted ancestor chain on `parent` is caught rather than
        # silently extended. Walk from `parent`'s existing parent (not `parent`
        # itself) - passing `parent` as both self and new_parent would trip the
        # identity check unconditionally, since any saved pin is trivially its
        # own pk match against itself.
        if parent.would_create_cycle(parent.parent_pin):
            return JsonResponse({"ok": False, "error": "Invalid parent pin."}, status=400)

        detail_name = body.get("name") or None
        pin_type, pin_type_chosen = _requested_pin_type(body)
        detail_pin = Pin.objects.create(
            name=detail_name,
            name_is_user_provided=bool((detail_name or "").strip()),
            description=body.get("description") or None,
            pin_type=pin_type,
            pin_type_is_user_provided=pin_type_chosen,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
            detail_bg_color=body.get("bg_color") or None,
            detail_bg_opacity=int(body.get("bg_opacity") or 80),
            detail_border_color=body.get("border_color") or None,
            detail_border_opacity=int(body.get("border_opacity") or 100),
            parent_pin=parent,
            profile=parent.profile,
            location=_location_for_coords(lat, lon),
        )
        if not pin_type_chosen:
            _schedule_classification("pin", detail_pin.pk)
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

        # Type is handled apart from the loop above: it is non-nullable (a
        # blank submission is the dialog's "Auto", not "clear it"), and a
        # re-pick has to update pin_type_is_user_provided alongside it.
        reclassify = False
        if "pin_type" in body:
            detail_pin.pin_type, detail_pin.pin_type_is_user_provided = _requested_pin_type(body)
            reclassify = not detail_pin.pin_type_is_user_provided

        new_latitude = body.get("latitude")
        new_longitude = body.get("longitude")
        if moved := bool(new_latitude and new_longitude):
            # A move repoints the detail pin to a Location at the new coordinates.
            detail_pin.location = _location_for_coords(new_latitude, new_longitude)

        detail_pin.save()
        # A moved auto-typed marker may have landed on (or left) a building, so
        # its classification is re-derived from wherever it now sits.
        if reclassify or (moved and not detail_pin.pin_type_is_user_provided):
            _schedule_classification("pin", detail_pin.pk)
        return JsonResponse({"ok": True})

    def delete(self, request, pin_slug, detail_pin_uuid):
        detail_pin = self._get_detail_pin(request, pin_slug, detail_pin_uuid)
        subtree = list(Pin.objects.filter(pk=detail_pin.pk).with_descendants())
        stash_for_undo("pin", subtree, detail_pin.profile)
        for descendant in subtree:
            descendant.delete()
        response = HttpResponse("", status=200)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": "Detail pin deleted. Undo within 7 days from Settings → Undo History."}})
        return response


class DetailPinJsonView(LoginRequiredMixin, View):
    """Return personal detail pins as JSON for Leaflet rendering on the pin details page.

    By default only the pin's direct children are returned. With ``?children=1``
    (the page-wide "show child pin details" toggle) the full descendant subtree is
    returned instead, each nested pin annotated with the name of the child pin
    it belongs to so the map can label it.
    """

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        include_children = request.GET.get("children") == "1"
        if include_children:
            detail_pins = pin.descendants().select_related("location", "parent_pin", "parent_pin__location").order_by("pin_type", "name")
        else:
            detail_pins = pin.detail_pins.select_related("location").order_by("pin_type", "name")

        payload = []
        for dp in detail_pins:
            entry = dp.to_detail_json()
            if include_children and dp.parent_pin_id != pin.pk and dp.parent_pin is not None:
                entry["owner_name"] = dp.parent_pin.effective_name
            payload.append(entry)
        return JsonResponse({"detail_pins": payload})


class LocationDetailPinJsonView(LoginRequiredMixin, View):
    """Return a wiki's child wikis as JSON for Leaflet rendering (map overlay).

    Child wikis are community sub-markers - buildings, entrances, points of
    interest, hazards - nested under a location's wiki via ``Wiki.parent_wiki``.
    Unlike the old Pin-backed community detail pins, a Wiki has no owning
    profile, so there is no per-viewer "added_by"/"is_mine" attribution.
    """

    def get(self, request, location_slug):
        location = get_object_or_404(Location.objects.slug_or_uuid(location_slug))
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not location_visible_to(location, profile):
            raise Http404
        try:
            wiki = location.wiki
        except ObjectDoesNotExist:
            # A location with no wiki yet simply has no child wikis to show -
            # the map overlay shouldn't error just because nobody has created
            # a wiki page for this spot.
            return JsonResponse({"detail_pins": []})
        child_wikis = wiki.child_wikis.order_by("pin_type", "name")
        return JsonResponse({"detail_pins": [cw.to_detail_json() for cw in child_wikis]})


class LocationWikiDetailPinView(LoginRequiredMixin, View):
    """Child wikis for a wiki page.

    GET  → renders the (legacy, currently unused by the wiki page's own JS) panel partial.
    POST → creates a new child wiki, records a WikiEdit, returns JSON.
    """

    def get(self, request, location_slug):
        location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        child_wikis = wiki.child_wikis.order_by("pin_type", "name")
        return render(
            request,
            "dashboard/partials/pins/location_detail_pins_panel.html",
            {
                "location": location,
                "wiki": wiki,
                "detail_pins": child_wikis,
                "pin_type_choices": PinType.choices,
            },
        )

    def post(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        lat = body.get("latitude")
        lon = body.get("longitude")
        if not lat or not lon:
            return JsonResponse({"ok": False, "error": "latitude and longitude required"}, status=400)

        # Defense-in-depth: this endpoint only ever creates a brand-new child
        # wiki (never re-parents an existing one), so a cycle can't actually
        # form here today. Guard anyway so this stays safe if that ever
        # changes, and so a pre-existing corrupted ancestor chain on `wiki`
        # is caught rather than silently extended. Walk from `wiki`'s existing
        # parent (not `wiki` itself) - see the matching Pin check for why.
        if wiki.would_create_cycle(wiki.parent_wiki):
            return JsonResponse({"ok": False, "error": "Invalid parent wiki."}, status=400)

        child_name = body.get("name") or wiki.name
        pin_type, pin_type_chosen = _requested_pin_type(body)
        child_wiki = Wiki.objects.create(
            name=child_name,
            description=body.get("description") or None,
            pin_type=pin_type,
            pin_type_is_user_provided=pin_type_chosen,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
            detail_bg_color=body.get("bg_color") or None,
            detail_bg_opacity=int(body.get("bg_opacity") or 80),
            detail_border_color=body.get("border_color") or None,
            detail_border_opacity=int(body.get("border_opacity") or 100),
            parent_wiki=wiki,
            location=_location_for_child_wiki(lat, lon),
        )

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"child_wiki_added": {"from": None, "to": child_wiki.name}},
        )

        if not pin_type_chosen:
            _schedule_classification("wiki", child_wiki.pk)
        return JsonResponse({"ok": True, "uuid": str(child_wiki.uuid)})


class LocationWikiDetailPinEditView(LoginRequiredMixin, View):
    """Edit, move, or delete a child wiki.

    Both verbs share one URL (mirroring the personal-pin equivalent,
    DetailPinEditView) so the frontend can use one base URL for both.
    Moves and deletes record a WikiEdit on the *parent* wiki.
    """

    def post(self, request, location_slug, detail_pin_uuid):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        child_wiki = get_object_or_404(Wiki, uuid=detail_pin_uuid, parent_wiki=wiki)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = request.POST

        # Style/content fields update silently (no WikiEdit) - same reasoning
        # as personal detail pins: these autosave on every panel change, and a
        # granular audit entry per keystroke would flood the wiki's edit history.
        # Unlike Pin.name, Wiki.name is required (non-null) - a blank submission
        # keeps the current name instead of clearing it.
        for field, value in {
            "name": body.get("name") or child_wiki.name,
            "description": body.get("description") or None,
            "icon": body.get("icon") or None,
            "color": body.get("color") or None,
            "detail_bg_color": body.get("bg_color") or None,
            "detail_border_color": body.get("border_color") or None,
        }.items():
            if value is not None or field in body:
                setattr(child_wiki, field, value)
        if "bg_opacity" in body:
            child_wiki.detail_bg_opacity = int(body["bg_opacity"])
        if "border_opacity" in body:
            child_wiki.detail_border_opacity = int(body["border_opacity"])

        # Type is handled apart from the loop above - see the matching comment
        # in DetailPinEditView for why.
        reclassify = False
        if "pin_type" in body:
            child_wiki.pin_type, child_wiki.pin_type_is_user_provided = _requested_pin_type(body)
            reclassify = not child_wiki.pin_type_is_user_provided

        new_latitude = body.get("latitude")
        new_longitude = body.get("longitude")
        old_lat, old_lon = child_wiki.location.latitude, child_wiki.location.longitude
        if moved := bool(new_latitude and new_longitude):
            child_wiki.location = _location_for_child_wiki(new_latitude, new_longitude)
        child_wiki.save()

        if reclassify or (moved and not child_wiki.pin_type_is_user_provided):
            _schedule_classification("wiki", child_wiki.pk)

        if moved:
            WikiEdit.objects.create(
                wiki=wiki,
                editor=profile,
                changes={
                    "child_wiki_moved": {
                        "pin": child_wiki.name,
                        "from": [str(old_lat), str(old_lon)],
                        "to": [str(child_wiki.location.latitude), str(child_wiki.location.longitude)],
                    }
                },
            )

        return JsonResponse({"ok": True})

    def delete(self, request, location_slug, detail_pin_uuid):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        child_wiki = get_object_or_404(Wiki, uuid=detail_pin_uuid, parent_wiki=wiki)

        child_name = child_wiki.name

        subtree = with_wiki_descendants([child_wiki])
        stash_for_undo("wiki", subtree, profile)
        for descendant in subtree:
            descendant.delete()

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"child_wiki_removed": {"from": child_name, "to": None}},
        )

        response = HttpResponse("", status=200)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": "Detail wiki deleted. Undo within 7 days from Settings → Undo History."}})
        return response
