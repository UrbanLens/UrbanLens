"""Pin List controllers - named, ordered collections of a profile's pins."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
import uuid as uuid_lib

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Prefetch, Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.filter_criteria import serialize_form_criteria
from urbanlens.dashboard.services.map_snapshot import materialize_markup_map
from urbanlens.dashboard.services.pin_list_markup import build_list_markup_snapshot
from urbanlens.dashboard.services.pin_list_membership import resync_smart_list
from urbanlens.dashboard.services.pin_list_trip import copy_list_pins_to_trip
from urbanlens.dashboard.services.text_limits import MAX_PIN_LIST_DESCRIPTION_LENGTH, text_length_error

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Bulk-add requests at or below this count never need confirmation.
_BULK_ADD_CONFIRM_THRESHOLD = 100

_ITEMS_PANEL_TEMPLATE = "dashboard/partials/pin_lists/_items_panel.html"


def _default_trip_name_for_list(pin_list: PinList) -> str:
    """Build the default trip name used when creating a trip from a pin list.

    Args:
        pin_list: The list the trip is being created from.

    Returns:
        "Trip from the "[name]" list" unless the list's own name already
        contains the word "list", in which case that would read redundantly
        (e.g. "Trip from the "My Bucket List" list"), so it falls back to
        "Trip from [name]".
    """
    if "list" in pin_list.name.lower():
        return f"Trip from {pin_list.name}"
    return f'Trip from the "{pin_list.name}" list'


def _list_items_with_badges(pin_list: PinList) -> list[PinListItem]:
    """Ordered list items with their pin's badges prefetched (icon/color/tag chips need these).

    Matches the same prefetch shape the main map's bulk pin endpoints use
    (see maps.py) so ``Pin.effective_icon``/``effective_color`` and the tag
    chip list resolve without N+1 queries.
    """
    return list(
        pin_list.items.select_related("pin", "pin__location")
        .prefetch_related(Prefetch("pin__badges", queryset=Badge.objects.exclude(kind="user").order_by("-order", "name")))
        .order_by("order"),
    )


def _pin_map_marker_data(pin: Pin) -> dict[str, Any]:
    """Serialize a pin for the list-detail overview map's badge-icon markers/popups.

    Mirrors the field shapes the main map's markers already expect (icon,
    tags_data, rating, last_visited as "Never" or "YYYY-MM-DD", etc. - see
    maps.py's post-processing of ``Pin.to_json()``) so the same marker/popup
    look carries over here. Reads ``pin.badges.all()`` (not ``.filter()``) so
    the ``pin__badges`` prefetch in ``_list_items_with_badges`` is reused
    instead of triggering a query per pin.
    """
    tags = [{"id": b.id, "name": b.name, "color": b.effective_color, "icon": b.effective_icon} for b in pin.badges.all() if b.kind == "tag"]
    return {
        "uuid": str(pin.uuid),
        "name": pin.effective_name,
        "url": reverse("pin.details", args=[pin.slug]) if pin.slug else "",
        "icon": pin.effective_icon,
        "color": pin.effective_color,
        "rating": pin.rating,
        "address": pin.effective_address or "",
        "description": pin.description or "",
        "last_visited": pin.last_visited.strftime("%Y-%m-%d") if pin.last_visited else "Never",
        "latitude": pin.effective_latitude,
        "longitude": pin.effective_longitude,
        "tags_data": tags,
    }


def _items_map_data(items: list[PinListItem]) -> list[dict[str, Any]]:
    return [_pin_map_marker_data(item.pin) for item in items if item.pin.effective_latitude and item.pin.effective_longitude]


def _render_items_panel(request: HttpRequest, pin_list: PinList) -> HttpResponse:
    items = _list_items_with_badges(pin_list)
    return render(request, _ITEMS_PANEL_TEMPLATE, {"pin_list": pin_list, "items": items, "items_map_data": _items_map_data(items)})


def _parse_body(request: HttpRequest) -> dict[str, Any]:
    try:
        return json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        return request.POST.dict()


class PinListsIndexView(LoginRequiredMixin, View):
    """Lists and Filters content - lives as tabs on the Organize page.

    GET /lists/?tab=lists|filters

    Direct browser navigation (no ``HX-Request`` header) redirects to the
    equivalent Organize tab - this URL now only serves the HTMX fragment that
    Organize's Lists/Filters tabs lazy-load into themselves the first time
    they're shown (see organize/index.html and organize.py's ``active_section``).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        active_tab = "filters" if request.GET.get("tab") == "filters" else "lists"
        if not request.headers.get("HX-Request"):
            return HttpResponseRedirect(f"{reverse('organize.index')}?tab={active_tab}")

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if active_tab == "filters":
            saved_filters = list(profile.saved_filters.all())
            return render(
                request,
                "dashboard/partials/pin_lists/_organize_filters_panel.html",
                {
                    "saved_filters": saved_filters,
                    **profile.get_map_center_template_context(),
                },
            )

        sort = request.GET.get("sort") or "updated"
        pin_lists = PinList.objects.filter(profile=profile).prefetch_related("items__pin")
        if sort == "name":
            pin_lists = pin_lists.order_by("name")
        elif sort == "pin_count":
            pin_lists = sorted(pin_lists, key=lambda pl: pl.pin_count, reverse=True)
        else:
            pin_lists = pin_lists.order_by("-updated")
        return render(
            request,
            "dashboard/partials/pin_lists/_organize_lists_panel.html",
            {
                "pin_lists": pin_lists,
                "sort": sort,
            },
        )


class PinListCreateView(LoginRequiredMixin, View):
    """Create a new list.

    POST /lists/create/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        body = _parse_body(request)

        name = (body.get("name") or "").strip()
        if not name:
            return HttpResponse("List name is required.", status=400)
        if PinList.objects.filter(profile=profile, name=name).exists():
            return HttpResponse("You already have a list with that name.", status=409)

        description = body.get("description") or ""
        length_error = text_length_error(description, MAX_PIN_LIST_DESCRIPTION_LENGTH, "Description")
        if length_error:
            return HttpResponse(length_error, status=400)

        pin_list = PinList.objects.create(profile=profile, name=name, description=description)

        if request.headers.get("Accept") == "application/json" or request.headers.get("HX-Request"):
            return JsonResponse({"ok": True, "uuid": str(pin_list.uuid), "name": pin_list.name, "redirect": reverse("lists.detail", kwargs={"list_uuid": pin_list.uuid})})
        return HttpResponseRedirect(reverse("lists.detail", kwargs={"list_uuid": pin_list.uuid}))


class PinListDetailView(LoginRequiredMixin, View):
    """List detail page.

    GET /lists/<uuid>/
    """

    def get(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        items = _list_items_with_badges(pin_list)
        saved_filters = list(profile.saved_filters.all())
        trips = list(Trip.objects.filter(profiles=profile).order_by("name"))
        return render(
            request,
            "dashboard/pages/pin_lists/detail.html",
            {
                "pin_list": pin_list,
                "items": items,
                "items_map_data": _items_map_data(items),
                "saved_filters": saved_filters,
                "trips": trips,
                **profile.get_map_center_template_context(),
                # The pins overview map uses the shared layers component, whose
                # base layer (and therefore attribution) can change at runtime -
                # see the footer partial's show_map_footer doc comment. The
                # boundary-drawing mini-map stays on a fixed OSM layer, which
                # happens to match that component's own default attribution.
                "show_map_footer": True,
            },
        )


class PinListEditView(LoginRequiredMixin, View):
    """Edit a list's name/description/smart configuration.

    POST /lists/<uuid>/edit/ → JSON ``{"ok": true}``

    Accepts a JSON body with any subset of ``name``, ``description``,
    ``is_smart``, ``saved_filter_uuid`` (copies that SavedFilter's criteria
    into ``smart_filter``; empty string clears it), and ``smart_boundary``
    (a GeoJSON Polygon/MultiPolygon geometry, or ``null`` to clear it - same
    payload shape as the pin/wiki boundary editor).
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        body = _parse_body(request)

        name = (body.get("name") or "").strip()
        if name:
            pin_list.name = name

        if "description" in body:
            description = body.get("description") or ""
            length_error = text_length_error(description, MAX_PIN_LIST_DESCRIPTION_LENGTH, "Description")
            if length_error:
                return HttpResponse(length_error, status=400)
            pin_list.description = description

        smart_config_changed = False
        if "is_smart" in body:
            pin_list.is_smart = str(body.get("is_smart")).strip().lower() in {"true", "1", "yes", "on"}
            smart_config_changed = True

        if "saved_filter_uuid" in body:
            saved_filter_uuid = (body.get("saved_filter_uuid") or "").strip()
            if saved_filter_uuid:
                saved_filter = get_object_or_404(SavedFilter, uuid=saved_filter_uuid, profile=profile)
                pin_list.smart_filter = saved_filter.criteria
            else:
                pin_list.smart_filter = None
            smart_config_changed = True

        if "smart_boundary" in body:
            from urbanlens.dashboard.services.geo import parse_multipolygon_geojson

            polygon_geojson = body.get("smart_boundary")
            if polygon_geojson:
                try:
                    pin_list.smart_boundary = parse_multipolygon_geojson(polygon_geojson)
                except (ValueError, TypeError) as exc:
                    return JsonResponse({"ok": False, "error": str(exc)}, status=400)
            else:
                pin_list.smart_boundary = None
            smart_config_changed = True

        pin_list.save()

        if smart_config_changed and pin_list.is_smart:
            resync_smart_list(pin_list)

        return JsonResponse({"ok": True, "pin_count": pin_list.pin_count})


class PinListDeleteView(LoginRequiredMixin, View):
    """Delete a list (items cascade).

    POST /lists/<uuid>/delete/
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        pin_list.delete()
        return HttpResponseRedirect(f"{reverse('organize.index')}?tab=lists")


class PinListItemsView(LoginRequiredMixin, View):
    """Items panel for a list.

    GET /lists/<uuid>/items/
    """

    def get(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        return _render_items_panel(request, pin_list)


class PinListAddPinsView(LoginRequiredMixin, View):
    """Add pins to a list, explicitly or by replaying filter criteria.

    POST /lists/<uuid>/items/add/

    Accepts either a ``pin_ids`` list (explicit selection - the pin-detail
    "add to list" flow sends a single id) or, when no ``pin_ids`` are given,
    ``SearchForm``-shaped POST fields (the map sidebar's "Add these pins to
    a list" flow, replaying whatever filters are currently active against
    the profile's full, unpaginated pin set). ``pin_slugs`` is also accepted
    (the list detail page's own pin search, which only has slugs on hand).
    Adding more than 100 new pins at once requires a follow-up POST with
    ``confirmed=true``.
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)

        pin_id_values = request.POST.getlist("pin_ids")
        # The shared location-search engine identifies pins by slug, falling back to
        # the uuid when a pin has no slug (see AutocompleteResult.pin_slug) - split
        # out anything that parses as a uuid so Pin.uuid (also a valid identifier
        # here) is matched too, instead of only Pin.slug.
        pin_slug_values = request.POST.getlist("pin_slugs")
        slug_values: list[str] = []
        uuid_values: list[str] = []
        for value in pin_slug_values:
            try:
                uuid_lib.UUID(value)
                uuid_values.append(value)
            except (ValueError, AttributeError, TypeError):
                slug_values.append(value)
        if pin_id_values or slug_values or uuid_values:
            pins = list(Pin.objects.filter(profile=profile).filter(Q(pk__in=pin_id_values) | Q(slug__in=slug_values) | Q(uuid__in=uuid_values)))
        else:
            search_form = SearchForm(request.POST, profile=profile)
            if not search_form.is_valid():
                return HttpResponse("Invalid filter criteria.", status=400)
            criteria = dict(search_form.cleaned_data)
            parsed_groups = search_form.parse_badge_groups()
            if parsed_groups is not None:
                criteria["badge_groups"] = parsed_groups
            if (custom_field_criteria := search_form.parse_custom_field_criteria()) is not None:
                criteria["custom_fields"] = custom_field_criteria
            criteria["include_regions"] = search_form.parse_region_geojson("include_regions")
            criteria["exclude_regions"] = search_form.parse_region_geojson("exclude_regions")
            pins = list(Pin.objects.filter(profile=profile).root_pins().filter_by_criteria(criteria))

        existing_pin_ids = set(pin_list.items.values_list("pin_id", flat=True))
        new_pins = [pin for pin in pins if pin.pk not in existing_pin_ids]

        if not new_pins:
            return _render_items_panel(request, pin_list)

        confirmed = request.POST.get("confirmed") == "true"
        if len(new_pins) > _BULK_ADD_CONFIRM_THRESHOLD and not confirmed:
            return JsonResponse({"confirm_required": True, "count": len(new_pins)}, status=409)

        base_order = pin_list.items.count()
        PinListItem.objects.bulk_create(
            [PinListItem(pin_list=pin_list, pin=pin, order=base_order + i, added_via=PinListItem.ADDED_MANUAL) for i, pin in enumerate(new_pins)],
        )
        return _render_items_panel(request, pin_list)


class PinListRemoveItemView(LoginRequiredMixin, View):
    """Remove a single pin from a list (explicit removal - always allowed).

    POST /lists/<uuid>/items/<id>/remove/
    """

    def post(self, request: HttpRequest, list_uuid: str, item_id: int) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        PinListItem.objects.filter(pin_list=pin_list, pk=item_id).delete()
        return _render_items_panel(request, pin_list)


class PinListReorderView(LoginRequiredMixin, View):
    """Persist a new item order.

    POST /lists/<uuid>/items/reorder/  body: ``{"items": [{"id": ...}, ...]}``
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        body = _parse_body(request)

        item_ids = [int(entry["id"]) for entry in body.get("items", []) if str(entry.get("id", "")).isdigit()]
        items_by_id = {item.pk: item for item in PinListItem.objects.filter(pin_list=pin_list, pk__in=item_ids)}

        updated = []
        for order, item_id in enumerate(item_ids):
            item = items_by_id.get(item_id)
            if item is None:
                continue
            item.order = order
            updated.append(item)
        if updated:
            PinListItem.objects.bulk_update(updated, ["order"])
        return HttpResponse(status=200)


class PinListCreateTripView(LoginRequiredMixin, View):
    """Create a new trip from a list's pins (one-time copy).

    POST /lists/<uuid>/create-trip/
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        body = _parse_body(request)

        trip_name = (body.get("name") or "").strip() or _default_trip_name_for_list(pin_list)
        trip = Trip.objects.create(name=trip_name, creator=profile)
        TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED})
        copy_list_pins_to_trip(pin_list, trip, profile)

        return JsonResponse({"ok": True, "redirect": reverse("trips.detail", kwargs={"trip_slug": trip.slug})})


class PinListAddToTripView(LoginRequiredMixin, View):
    """Add a list's pins onto an existing trip (one-time copy, appended).

    POST /lists/<uuid>/add-to-trip/  body: ``{"trip_slug": ...}``
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        from urbanlens.dashboard.controllers.trip import _trip_or_403

        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)
        body = _parse_body(request)

        trip_slug = body.get("trip_slug")
        if not trip_slug:
            return HttpResponse("A trip is required.", status=400)
        result = _trip_or_403(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        count = copy_list_pins_to_trip(pin_list, trip, profile)
        return JsonResponse({"ok": True, "added": count, "redirect": reverse("trips.detail", kwargs={"trip_slug": trip.slug})})


class PinListMarkupMapView(LoginRequiredMixin, View):
    """Create or refresh a markup map showing every pin on a list.

    POST /lists/<uuid>/markup-map/
    """

    def post(self, request: HttpRequest, list_uuid: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_list = get_object_or_404(PinList, uuid=list_uuid, profile=profile)

        snapshot = build_list_markup_snapshot(pin_list)
        if snapshot is None:
            return JsonResponse({"ok": False, "error": "This list has no pins with map coordinates yet."}, status=400)

        markup_map = materialize_markup_map(profile, snapshot, existing_map=pin_list.markup_map)
        if markup_map is None:
            # materialize_markup_map only returns None when the snapshot itself is
            # None (map removed) - unreachable here since we already checked above,
            # but handled explicitly rather than assumed.
            return JsonResponse({"ok": False, "error": "Unable to create markup map."}, status=500)

        if pin_list.markup_map_id != markup_map.pk:
            pin_list.markup_map = markup_map
            pin_list.save(update_fields=["markup_map", "updated"])

        return JsonResponse({"ok": True, "redirect": reverse("markup_map.markup", kwargs={"map_uuid": markup_map.uuid})})
