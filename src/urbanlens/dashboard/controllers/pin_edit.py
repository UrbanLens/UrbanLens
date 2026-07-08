"""Pin inline-edit and personal notes controllers."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import DatabaseError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.locations.naming import sync_pin_aliases_after_rename

logger = logging.getLogger(__name__)


def _pin_for_user(pin_slug, request) -> Pin | HttpResponse:
    """Return the pin if it belongs to the requesting user.

    Returns 403 when the requester has no authenticated profile at all. Any
    other user's pin - whether it exists or not - returns 404: the lookup is
    scoped to the requester's own profile, so a pin owned by someone else is
    indistinguishable from a nonexistent one and its existence is never leaked.
    """
    if not request.user.is_authenticated or not request.user.profile:
        return HttpResponse("Forbidden", status=403)
    try:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile__user"), slug=pin_slug, profile=request.user.profile)
    except Http404:
        return HttpResponse(status=404)
    return pin


def _pin_version(pin: Pin) -> str:
    """Return an opaque version token for the pin's last-saved state.

    Clients echo this back on quick-edit requests (star clicks) so the server can tell
    whether anything else changed since that client last rendered the pin - see
    PinEditView.post for how this drives the minimal vs. full-resync response.
    """
    return str(int(pin.updated.timestamp())) if pin.updated else ""


# Metadata for the four single-field 1-5 star-rating widgets, shared between the full
# overview render and the minimal single-field response in PinEditView.post.
STAT_FIELD_META = {
    "danger": {
        "label": "Danger",
        "help": "How hazardous this site feels - structural risks, environmental hazards, or unsafe conditions (1 = low, 5 = extreme).",
        "modifier": "danger",
        "wide": True,
    },
    "priority": {
        "label": "Priority",
        "help": "How urgently you want to visit this pin (1 = low, 5 = must visit soon).",
        "modifier": "priority",
        "wide": False,
    },
    "rating": {
        "label": "Rating",
        "help": "Your quality rating for this location.",
        "modifier": "",
        "wide": False,
    },
    "vulnerability": {
        "label": "Vulnerability",
        "help": "How at-risk or fragile this site feels - useful for planning and sharing responsibly.",
        "modifier": "vulnerability",
        "wide": True,
    },
}


def _stat_item_context(pin: Pin, field: str) -> dict:
    return {"pin": pin, "field": field, "client_version": _pin_version(pin), **STAT_FIELD_META[field]}


def _overview_context(pin: Pin) -> dict:
    from urbanlens.dashboard.models.badges.model import COLOR_CHOICES
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import PinType

    detail_pin_icon_choices = [
        ("place", "Place"),
        ("business", "Building"),
        ("door_front", "Entrance"),
        ("star", "Star"),
        ("warning", "Warning"),
        ("info", "Info"),
        ("camera_alt", "Camera"),
        ("local_parking", "Parking"),
        ("stairs", "Stairs"),
        ("elevator", "Elevator"),
        ("exit_to_app", "Exit"),
        ("lock", "Lock"),
        ("construction", "Construction"),
        ("emergency", "Emergency"),
    ]

    lat, lng = pin.effective_latitude, pin.effective_longitude
    overlapping_location_count = Location.objects.get_all_for_point(float(lat), float(lng)).count() if lat is not None and lng is not None else 0

    return {
        "pin": pin,
        "client_version": _pin_version(pin),
        "pin_type_choices": PinType.choices,
        "all_categories": Badge.objects.categories().ordered(),
        "detail_pin_icon_choices": detail_pin_icon_choices,
        "color_choices": COLOR_CHOICES,
        "security_level_choices": SecurityLevel.choices,
        "overlapping_location_count": overlapping_location_count,
        "pin_security_values": [
            ("fences", "Fences", pin.fences),
            ("alarms", "Alarms", pin.alarms),
            ("cameras", "Cameras", pin.cameras),
            ("security", "Security", pin.security),
            ("signs", "Signs", pin.signs),
            ("vps", "VPS", pin.vps),
            ("plywood", "Plywood", pin.plywood),
            ("locked", "Locked", pin.locked),
        ],
    }


def _ensure_location_address(location) -> None:
    """Populate address fields on a Location that has coordinates but no street data.

    Calls the Google Geocoding API (with GeocodedLocation as an intermediate cache),
    then writes the parsed components back to the Location row so the next request
    reads directly from the DB with no API call.
    """
    if not location or location.route:
        return
    lat = float(location.latitude) if location.latitude is not None else None
    lng = float(location.longitude) if location.longitude is not None else None
    if lat is None or lng is None:
        return

    try:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.google_unrestricted_api_key:
            return

        data = GoogleGeocodingGateway().geocode_coordinates(lat, lng)
        if not data:
            return
        results = data.get("results", [])
        if not results:
            return

        type_map: dict[str, str] = {}
        for comp in results[0].get("address_components", []):
            for t in comp.get("types", []):
                type_map.setdefault(t, comp.get("short_name") or comp.get("long_name") or "")

        update_fields: list[str] = []

        def _maybe_set(field: str, value: str | None) -> None:
            if value and not getattr(location, field):
                setattr(location, field, value)
                update_fields.append(field)

        _maybe_set("street_number", type_map.get("street_number"))
        _maybe_set("route", type_map.get("route"))
        _maybe_set("locality", type_map.get("locality"))
        _maybe_set("administrative_area_level_1", type_map.get("administrative_area_level_1"))
        _maybe_set("administrative_area_level_2", type_map.get("administrative_area_level_2"))
        _maybe_set("zipcode", type_map.get("postal_code"))

        if update_fields:
            location.save(update_fields=update_fields)
    except (ImportError, OSError, ValueError, DatabaseError):
        logger.exception("Reverse geocoding failed for location pk=%s", getattr(location, "pk", None))


class PinOverviewView(LoginRequiredMixin, View):
    """Render the swappable pin overview partial (title + details card).

    GET /map/pin/<uuid>/overview/
    """

    def get(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        if pin.location and not pin.location.route:
            _ensure_location_address(pin.location)
        return render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))


class PinEditView(LoginRequiredMixin, View):
    """Update editable pin fields.

    POST /map/pin/<uuid>/edit/
    Re-renders the pin overview partial on success.
    """

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        # Snapshot of what the client believed the pin's state was when it sent this
        # request, captured before any of this request's own changes are applied. Used
        # below to detect whether another tab/session changed the pin in the meantime.
        client_version = body.get("client_version")
        pre_save_version = _pin_version(pin)

        from datetime import date, datetime

        # Scalar fields. Star-rating widgets and other quick-edit controls submit
        # only the one field they changed, so any field absent from the body must
        # fall back to the pin's current value - never silently clear it.
        name = (body.get("name") or "").strip() or None if "name" in body else pin.name
        description = (body.get("description") or "").strip() or None if "description" in body else pin.description
        pin_type = body.get("pin_type") or pin.pin_type
        priority_raw = body.get("priority")
        rating_raw = body.get("rating")
        vulnerability_raw = body.get("vulnerability")
        danger_raw = body.get("danger")
        last_visited_raw = (body.get("last_visited") or "").strip() or None

        try:
            if priority_raw is not None and str(priority_raw).strip():
                p = int(priority_raw)
                priority = p if 0 <= p <= 5 else pin.priority
            else:
                priority = pin.priority
        except (TypeError, ValueError):
            priority = pin.priority

        try:
            if rating_raw is not None and str(rating_raw).strip():
                rating = int(rating_raw)
                if not (0 <= rating <= 5):
                    rating = pin.rating
                elif rating == 0:
                    rating = None
            else:
                rating = pin.rating
        except (TypeError, ValueError):
            rating = pin.rating

        try:
            if vulnerability_raw is not None and str(vulnerability_raw).strip():
                v = int(vulnerability_raw)
                vulnerability = v if 0 <= v <= 5 else pin.vulnerability
            else:
                vulnerability = pin.vulnerability
        except (TypeError, ValueError):
            vulnerability = pin.vulnerability

        try:
            if danger_raw is not None and str(danger_raw).strip():
                d = int(danger_raw)
                danger = d if 0 <= d <= 5 else pin.danger
            else:
                danger = pin.danger
        except (TypeError, ValueError):
            danger = pin.danger

        last_visited = None
        if last_visited_raw:
            for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                try:
                    last_visited = datetime.strptime(last_visited_raw, fmt)
                    break
                except ValueError:
                    continue
            if last_visited:
                today = date.today()
                min_date = date(today.year - 100, today.month, today.day)
                lv_date = last_visited.date()
                if lv_date > today:
                    return HttpResponse("Last visited date must be in the past.", status=400)
                if lv_date < min_date:
                    return HttpResponse("Last visited date must be within the last 100 years.", status=400)

        # Security indicators
        valid_security = {v for v, _ in SecurityLevel.choices}
        security_fields = ["fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked"]
        security_values = {}
        for sf in security_fields:
            raw = body.get(sf, "")
            security_values[sf] = raw if raw in valid_security else getattr(pin, sf)

        # Abandonment dates
        def _parse_date(raw: str) -> date | None:
            raw = (raw or "").strip()
            if not raw:
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return None

        date_abandoned = _parse_date(body.get("date_abandoned", "")) if "date_abandoned" in body else pin.date_abandoned
        date_last_active = _parse_date(body.get("date_last_active", "")) if "date_last_active" in body else pin.date_last_active

        # Validate choices
        valid_types = {v for v, _ in PinType.choices}
        if pin_type not in valid_types:
            pin_type = pin.pin_type

        previous_name = (pin.name or "").strip()
        next_name = (name or "").strip()

        pin.name = name
        pin.name_is_user_provided = bool(next_name)
        pin.description = description
        pin.pin_type = pin_type
        pin.priority = priority
        pin.vulnerability = vulnerability
        pin.danger = danger
        if last_visited is not None:
            pin.last_visited = last_visited
        for sf, val in security_values.items():
            setattr(pin, sf, val)
        pin.date_abandoned = date_abandoned
        pin.date_last_active = date_last_active
        pin.save(
            update_fields=[
                "name",
                "name_is_user_provided",
                "description",
                "pin_type",
                "priority",
                "vulnerability",
                "danger",
                "last_visited",
                "fences",
                "alarms",
                "cameras",
                "security",
                "signs",
                "vps",
                "plywood",
                "locked",
                "date_abandoned",
                "date_last_active",
                "updated",
            ]
        )

        sync_pin_aliases_after_rename(pin, previous_name)

        # rating lives on the Review model (one review per user per pin)
        if rating and 1 <= rating <= 5:
            Review.objects.update_or_create(
                user=request.user,
                pin=pin,
                defaults={"rating": rating, "review": ""},
            )
        elif rating == 0:
            Review.objects.filter(user=request.user, pin=pin).delete()

        # Category update: only runs when the field was explicitly submitted (partial requests preserve existing)
        if "categories" in body:
            category_raw = (body.get("categories") or "").strip()
            names = [n.strip().lower() for n in category_raw.split(",") if n.strip()]
            seen_names: set[str] = set()
            pin.badges.remove(*pin.badges.filter(kind="category"))
            for name in names:
                if name in seen_names:
                    continue
                seen_names.add(name)
                cat = Badge.objects.filter(name__iexact=name, kind="category", profile=pin.profile).first()
                if cat is None:
                    cat, _ = Badge.objects.get_or_create(
                        name=name,
                        kind="category",
                        profile=pin.profile,
                    )
                pin.badges.add(cat)

        # Reload from DB so all properties reflect saved state
        pin.refresh_from_db()
        pin.badges.filter(kind="category")  # prime M2M cache

        # Quick-edit widgets (star ratings) submit exactly one field at a time. When the
        # client's last-known version still matches what was in the DB before this save,
        # nothing else has drifted, so we only need to send back the one fragment that
        # changed - this is the common case and keeps these frequent requests tiny.
        # If something else changed (e.g. a different tab edited the name), fall back to
        # a full resync: the small fragment still satisfies the primary hx-target swap,
        # and an out-of-band re-render of the whole card brings everything else current.
        submitted_fields = set(body.keys()) - {"client_version"}
        if len(submitted_fields) == 1 and submitted_fields <= set(STAT_FIELD_META):
            field = next(iter(submitted_fields))
            fragment = render(request, "dashboard/partials/pins/_pin_stat_rating_item.html", _stat_item_context(pin, field))
            if client_version is not None and client_version == pre_save_version:
                return fragment
            oob_context = {**_overview_context(pin), "oob": True}
            oob = render(request, "dashboard/partials/pins/pin_overview_partial.html", oob_context)
            return HttpResponse(fragment.content + oob.content)

        return render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))


class PinNotesView(LoginRequiredMixin, View):
    """Personal notes panel for a pin.

    GET  /map/pin/<uuid>/notes/  → render panel
    POST /map/pin/<uuid>/notes/  → add note, re-render panel
    """

    def get(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        notes = pin.notes.order_by("-created")
        return render(request, "dashboard/partials/pins/pin_notes_panel.html", {"pin": pin, "notes": notes})

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        text = (body.get("text") or "").strip()
        if not text:
            return HttpResponse("Note text is required.", status=400)

        PinNote.objects.create(pin=pin, text=text)
        notes = pin.notes.order_by("-created")
        return render(request, "dashboard/partials/pins/pin_notes_panel.html", {"pin": pin, "notes": notes})


class PinNoteDeleteView(LoginRequiredMixin, View):
    """Delete a single personal note.

    DELETE /map/pin/<uuid>/notes/<int:note_id>/delete/
    """

    def delete(self, request, pin_slug, note_id):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        note = get_object_or_404(PinNote, id=note_id, pin=pin)
        note.delete()
        return HttpResponse("", status=200)


class PinDeleteView(LoginRequiredMixin, View):
    """Delete a pin owned by the current user.

    DELETE /map/pin/<pin_slug>/delete/

    Returns a 200 with an HX-Redirect header so HTMX navigates to the map after deletion.
    """

    def delete(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        logger.info("User %s deleted pin %s", request.user.id, pin.id)
        pin.delete()
        response = HttpResponse("", status=200)
        response["HX-Redirect"] = reverse("map.view")
        return response


class PinRelinkView(LoginRequiredMixin, View):
    """Link a pin to a different Location, or detach it to its own bare Location.

    GET  /map/pin/<uuid>/link/               → HTML picker listing all overlapping Locations
    POST /map/pin/<uuid>/link/               → Detach: creates a new Location at the pin's point
    POST /map/pin/<uuid>/link/<loc_uuid>/    → Relink: switches the pin to the given Location
    """

    def get(self, request, pin_slug):
        """Return an HTMX partial listing every Location that covers this pin's point.

        Args:
            request: The HTTP request.
            pin_slug: UUID of the pin.

        Returns:
            Rendered HTML partial with location choices.
        """
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        from urbanlens.dashboard.models.location.model import Location

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        locations = Location.objects.get_all_for_point(float(lat), float(lng)) if lat is not None and lng is not None else Location.objects.none()
        return render(
            request,
            "dashboard/partials/pins/pin_location_picker.html",
            {"pin": pin, "locations": locations},
        )

    def post(self, request, pin_slug, location_slug=None):
        """Relink or detach the pin.

        Args:
            request: The HTTP request.
            pin_slug: UUID of the pin.
            location_slug: Optional UUID of an existing Location to link to.
                If omitted, detaches the pin (creates a fresh bare Location).

        Returns:
            Re-rendered pin overview partial.
        """
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki

        if location_slug:
            location = get_object_or_404(Location, slug=location_slug)
        else:
            # Detach: create a new bare Location at this pin's coordinates so
            # the pin retains its own independent community wiki page.
            # Use the existing location's canonical name if available; otherwise
            # fetch the Google place name.  Never fall back to pin.name -
            # that is personal data and must not become a community wiki title.
            lat = float(pin.effective_latitude or 0)
            lng = float(pin.effective_longitude or 0)
            if pin.location and pin.location.official_name and pin.location.official_name != "Unnamed Location":
                location = Location.objects.create(
                    official_name=pin.location.official_name,
                    latitude=lat,
                    longitude=lng,
                )
            else:
                from urbanlens.dashboard.controllers.maps import _create_location_with_canonical_name

                location = _create_location_with_canonical_name(lat, lng)

        wiki, _ = Wiki.objects.get_or_create_for_location(location)
        pin.location = location
        pin.wiki = wiki
        pin.save(update_fields=["location", "wiki"])
        pin.refresh_from_db()

        return render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))
