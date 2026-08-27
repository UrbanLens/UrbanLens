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
from django.contrib.gis.geos import MultiPolygon
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
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

    children = {
        child.location.place_id: child
        for child in pin.detail_pins.select_related("location")
        if child.location_id and child.location.place_id
    }
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


def _building_outline(pin: Pin) -> list[list[float]]:
    """The building's footprint as a ring of ``[lat, lng]`` pairs, or empty.

    Handed to the editor so a new floor can start from the real exterior
    instead of asking someone to trace a wall they can already see.

    **Only a BUILDING boundary is used.** The property boundary is deliberately
    not a fallback: it describes the grounds, so on a campus or any lot larger
    than its structure it seeds an enormous room shaped like the parcel, which
    is both wrong and confidently wrong - it looks like survey data. An empty
    canvas is the better answer when no footprint is known.

    Returns:
        The outer ring, without the closing duplicate point (walls are drawn
        around it cyclically), or an empty list when no footprint is known.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    for boundary_type in (BoundaryType.BUILDING,):
        polygon = Boundary.objects.effective_polygon_for_pin(pin, boundary_type)
        if polygon is None:
            continue
        # A MultiPolygon here means several detached structures; the largest is
        # the one a floorplan is most likely about. isinstance rather than
        # geom_type, so this narrows for a reader and for the type checker
        # instead of only for the runtime - a Polygon is not iterable, and
        # nothing but the string said so.
        if isinstance(polygon, MultiPolygon):
            parts = sorted(polygon, key=lambda part: part.area, reverse=True)
            if not parts:
                continue
            polygon = parts[0]
        ring = getattr(polygon, "exterior_ring", None)
        if ring is None:
            continue
        points = [[round(y, 7), round(x, 7)] for x, y in list(ring)]
        # GEOS repeats the first point to close the ring; the editor closes it
        # itself by walking the points cyclically.
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) >= 3:
            return points
    return []


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
        from urbanlens.dashboard.services.floorplans.serialization import document_for

        pin = get_object_or_404(
            Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user
        )
        place = _building_place(pin)
        if place is None:
            # A placeless plan belongs to this pin alone, so there is no
            # community/REData cascade to consult - just the user's own row.
            from urbanlens.dashboard.models.floorplans.model import Floorplan

            own = (
                Floorplan.objects.filter(place__isnull=True, pin=pin, profile=pin.profile).order_by("-created").first()
            )
            if own is None:
                return HttpResponse(status=204)
            # Its own binding: this path always has a document, while the
            # place-backed one below resolves to None when nothing matches.
            own_document = {**document_for(own), "origin": "local", "versions": []}
            return JsonResponse(own_document)
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
        from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanMarkerKind, FloorplanWallKind
        from urbanlens.dashboard.services.floorplans.features import feature_collection
        from urbanlens.dashboard.services.floorplans.resolution import resolve_floorplan_row
        from urbanlens.dashboard.services.wiki.wiki_access import place_visible_to

        pin = get_object_or_404(
            Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user
        )
        place = _building_place(pin)
        if place is None:
            return HttpResponse(status=204)

        version_uuid = request.GET.get("version") or ""
        if version_uuid:
            # A specific version's uuid may name the caller's own plan or a
            # published one - same fallback as the unversioned lookup below,
            # just pinned to one exact row instead of "current".
            floorplan = Floorplan.objects.filter(
                place=place, profile=pin.profile, uuid=version_uuid, wiki__isnull=True
            ).first()
            if floorplan is None and place_visible_to(place, pin.profile):
                floorplan = Floorplan.objects.filter(place=place, uuid=version_uuid, wiki__isnull=False).first()
        else:
            # Local-then-community, mirroring resolve_document() - a
            # published plan must be reachable here too, not just through the
            # document endpoint. See services.floorplans.resolution.
            floorplan = resolve_floorplan_row(place, profile=pin.profile, on_date=_parse_date(request.GET.get("date")))
        if floorplan is None:
            return HttpResponse(status=204)

        try:
            bbox = _parse_bbox(request.GET.get("bbox"))
        except ValueError as exc:
            logger.warning("Unable to parse bbox: %s", str(exc))
            return JsonResponse({"ok": False, "error": "Unable to parse bbox"}, status=400)
        try:
            level = int(request.GET["level"]) if request.GET.get("level") else None
        except ValueError:
            # int()'s message quotes the caller's input back verbatim, which the
            # client renders into a toast - do not echo it.
            return JsonResponse({"ok": False, "error": "level must be a whole number."}, status=400)

        # One filter serves both wall kinds and marker kinds - they never
        # collide, and a caller asking for "stair" means markers whether or not
        # it knows which table that is.
        allowed = set(FloorplanWallKind.values) | set(FloorplanMarkerKind.values)
        kind = request.GET.get("kind", "")
        if kind and kind not in allowed:
            return JsonResponse({"ok": False, "error": f"kind must be one of {sorted(allowed)}"}, status=400)

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
        from urbanlens.dashboard.services.floorplans.serialization import (
            StaleDocumentError,
            document_for,
            save_document,
        )

        pin = get_object_or_404(
            Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user
        )
        # A plan may be placeless: not every pin resolves to a known building
        # outline, and refusing to save one made the editor a dead end for
        # exactly the properties that most need mapping by hand.
        place = _building_place(pin)

        try:
            document = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
        if not isinstance(document, dict):
            return JsonResponse({"ok": False, "error": "Expected a document object."}, status=400)

        floorplan = floorplan_for_editing(
            place,
            pin.profile,
            pin=pin,
            version_uuid=str(document.get("uuid") or ""),
            on_date=_parse_date(document.get("valid_from")),
            allow_community=bool(document.get("edit_community")),
        )
        # A wiki-owned row is shared: parenting it to whoever happened to save
        # it next would hand one profile's private pin the whole community
        # plan, and mint detail pins in that account for other people's
        # markers (see serialization._sync_linked_pin's stated invariant).
        if floorplan.pin_id is None and floorplan.wiki_id is None:
            floorplan.pin = pin
        # Seed the plan-local origin from the pin the first time anything is
        # saved. Every coordinate in the document is metres from this point, so
        # it has to be fixed before the first wall lands and must never move
        # afterwards - shifting it would silently translate the whole drawing.
        if floorplan.origin_lat is None or floorplan.origin_lng is None:
            document.setdefault(
                "plan_origin", {"lat": float(pin.effective_latitude), "lng": float(pin.effective_longitude)}
            )
        try:
            save_document(floorplan, document, profile=pin.profile)
        except StaleDocumentError as exc:
            # 409, not 400: the document is valid, it is just built on a version
            # someone else has already replaced. The editor stops autosaving on
            # this rather than retrying, since retrying is how the other tab's
            # work gets destroyed.
            return JsonResponse({"ok": False, "error": str(exc), "stale": True}, status=409)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        # A placeless plan has no sibling versions to list: for_place(None)
        # would match every placeless plan on the site rather than none.
        versions = _version_list(place, pin.profile) if place is not None else []
        # Real provenance, not an assumption: a hardcoded "local" told the
        # client it owned a row it had merely been allowed to edit, which hid
        # the community banner from the next render.
        origin = "community" if floorplan.wiki_id is not None else "local"
        saved = {**document_for(floorplan), "origin": origin, "versions": versions}
        return JsonResponse({"ok": True, "floorplan": saved})


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

        pin = get_object_or_404(
            Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user
        )
        place = _building_place(pin)
        if place is None:
            return JsonResponse({"ok": False, "error": "This pin has no single building."}, status=400)

        version_uuid = request.POST.get("version") or (
            json.loads(request.body or b"{}").get("uuid") if request.body else ""
        )
        floorplan = Floorplan.objects.filter(
            place=place, uuid=version_uuid or None, profile=pin.profile, wiki__isnull=True
        ).first()
        if floorplan is None:
            return JsonResponse({"ok": False, "error": "Save your floorplan before publishing it."}, status=404)

        community = publish_to_wiki(floorplan, pin.profile)
        if community is None:
            return JsonResponse(
                {"ok": False, "error": "This place has no community wiki yet - create one from the pin page first."},
                status=409,
            )
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
        from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES, ICON_CATEGORIES
        from urbanlens.dashboard.models.labels.model import Label

        context["pin"] = pin
        # The same icon set and palette every other picker on the site uses, so
        # a marker styled here and a detail pin styled from the pin page cannot
        # offer different choices.
        context["icon_categories"] = ICON_CATEGORIES
        context["color_choices"] = COLOR_CHOICES
        context["place"] = _building_place(pin)
        context["building_choices"] = _building_choices(pin) if context["place"] is None else []
        context["outline_json"] = _building_outline(pin)
        context["overlays_json"] = overlay_payload(pin.image_overlays.all())
        context["labels_json"] = [
            {"uuid": str(label.uuid), "name": label.name}
            for label in Label.objects.filter(profile=pin.profile).order_by("name")
        ]
        # The reference pool attaches to every item, so the photos already on
        # this pin are the likeliest evidence for a wall, a door or its lock.
        context["photos_json"] = [
            {"uuid": str(image.uuid), "url": image.image.url, "caption": image.caption or ""}
            for image in pin.images.order_by("-created")[:60]
            if image.image
        ]
        # Same "Manage Image Overlays" dialog as the pin-detail and wiki maps -
        # browsing this pin's own photos for a blueprint/site plan to pin and
        # skew onto the floorplan map, not a bespoke picker.
        context["manage_overlays_url"] = reverse("pin.overlays", args=[pin.slug])
        # attributionControl is off on this map; required attribution renders
        # in the page footer instead (see createMapLayers' onAttribution).
        context["show_map_footer"] = True
        return context
