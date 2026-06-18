"""Pin inline-edit and personal notes controllers."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin, PinNote, PinType

logger = logging.getLogger(__name__)


def _pin_for_user(pin_uuid, request) -> Pin | HttpResponse:
    """Return the pin if it belongs to the requesting user, else 403."""
    pin = get_object_or_404(Pin.objects.select_related("location", "profile__user"), uuid=pin_uuid)
    if pin.profile.user != request.user:
        return HttpResponse("Forbidden", status=403)
    return pin


def _overview_context(pin: Pin) -> dict:
    from urbanlens.dashboard.models.badges.model import COLOR_CHOICES
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import PinType

    detail_pin_icon_choices = [
        ("place", "Place"), ("business", "Building"), ("door_front", "Entrance"),
        ("star", "Star"), ("warning", "Warning"), ("info", "Info"),
        ("camera_alt", "Camera"), ("local_parking", "Parking"),
        ("stairs", "Stairs"), ("elevator", "Elevator"),
        ("exit_to_app", "Exit"), ("lock", "Lock"),
        ("construction", "Construction"), ("emergency", "Emergency"),
    ]

    lat, lng = pin.effective_latitude, pin.effective_longitude
    overlapping_location_count = (
        Location.objects.get_all_for_point(float(lat), float(lng)).count()
        if lat is not None and lng is not None
        else 0
    )

    return {
        "pin": pin,
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


class PinOverviewView(LoginRequiredMixin, View):
    """Render the swappable pin overview partial (title + details card).

    GET /map/pin/<uuid>/overview/
    """

    def get(self, request, pin_uuid):
        result = _pin_for_user(pin_uuid, request)
        if isinstance(result, HttpResponse):
            return result
        return render(request, "dashboard/partials/pin_overview_partial.html", _overview_context(result))


class PinEditView(LoginRequiredMixin, View):
    """Update editable pin fields.

    POST /map/pin/<uuid>/edit/
    Re-renders the pin overview partial on success.
    """

    def post(self, request, pin_uuid):
        result = _pin_for_user(pin_uuid, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        from datetime import date, datetime

        # Scalar fields
        nickname = (body.get("nickname") or "").strip() or None
        description = (body.get("description") or "").strip() or None
        pin_type = body.get("pin_type") or pin.pin_type
        priority_raw = body.get("priority")
        rating_raw = body.get("rating")
        last_visited_raw = (body.get("last_visited") or "").strip() or None

        try:
            if priority_raw is not None and str(priority_raw).strip():
                p = int(priority_raw)
                priority = None if p == 0 else p
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

        pin.nickname = nickname
        pin.description = description
        pin.pin_type = pin_type
        pin.priority = priority
        pin.rating = rating
        if last_visited is not None:
            pin.last_visited = last_visited
        for sf, val in security_values.items():
            setattr(pin, sf, val)
        pin.date_abandoned = date_abandoned
        pin.date_last_active = date_last_active
        pin.save(update_fields=[
            "nickname", "description", "pin_type", "priority", "rating", "last_visited",
            "fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked",
            "date_abandoned", "date_last_active", "updated",
        ])

        # Category update: comma-separated names replace all current categories
        category_raw = (body.get("categories") or "").strip()
        if category_raw is not None:
            names = [n.strip().lower() for n in category_raw.split(",") if n.strip()]
            pin.categories.clear()
            for name in names:
                cat, _ = Badge.objects.get_or_create(name=name, kind="category", defaults={"profile": None})
                pin.categories.add(cat)

        # Reload from DB so all properties reflect saved state
        pin.refresh_from_db()
        pin.categories.all()  # prime M2M cache

        return render(request, "dashboard/partials/pin_overview_partial.html", _overview_context(pin))


class PinNotesView(LoginRequiredMixin, View):
    """Personal notes panel for a pin.

    GET  /map/pin/<uuid>/notes/  → render panel
    POST /map/pin/<uuid>/notes/  → add note, re-render panel
    """

    def get(self, request, pin_uuid):
        result = _pin_for_user(pin_uuid, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        notes = pin.notes.order_by("-created")
        return render(request, "dashboard/partials/pin_notes_panel.html", {"pin": pin, "notes": notes})

    def post(self, request, pin_uuid):
        result = _pin_for_user(pin_uuid, request)
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
        return render(request, "dashboard/partials/pin_notes_panel.html", {"pin": pin, "notes": notes})


class PinNoteDeleteView(LoginRequiredMixin, View):
    """Delete a single personal note.

    DELETE /map/pin/<uuid>/notes/<int:note_id>/delete/
    """

    def delete(self, request, pin_uuid, note_id):
        result = _pin_for_user(pin_uuid, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        note = get_object_or_404(PinNote, id=note_id, pin=pin)
        note.delete()
        return HttpResponse("", status=200)


class PinRelinkView(LoginRequiredMixin, View):
    """Link a pin to a different Location, or detach it to its own bare Location.

    GET  /map/pin/<uuid>/link/               → HTML picker listing all overlapping Locations
    POST /map/pin/<uuid>/link/               → Detach: creates a new Location at the pin's point
    POST /map/pin/<uuid>/link/<loc_uuid>/    → Relink: switches the pin to the given Location
    """

    def get(self, request, pin_uuid):
        """Return an HTMX partial listing every Location that covers this pin's point.

        Args:
            request: The HTTP request.
            pin_uuid: UUID of the pin.

        Returns:
            Rendered HTML partial with location choices.
        """
        result = _pin_for_user(pin_uuid, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        from urbanlens.dashboard.models.location.model import Location

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        locations = (
            Location.objects.get_all_for_point(float(lat), float(lng))
            if lat is not None and lng is not None
            else Location.objects.none()
        )
        return render(
            request,
            "dashboard/partials/pin_location_picker.html",
            {"pin": pin, "locations": locations},
        )

    def post(self, request, pin_uuid, location_uuid=None):
        """Relink or detach the pin.

        Args:
            request: The HTTP request.
            pin_uuid: UUID of the pin.
            location_uuid: Optional UUID of an existing Location to link to.
                If omitted, detaches the pin (creates a fresh bare Location).

        Returns:
            Re-rendered pin overview partial.
        """
        result = _pin_for_user(pin_uuid, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        from urbanlens.dashboard.models.location.model import Location

        if location_uuid:
            location = get_object_or_404(Location, uuid=location_uuid)
        else:
            # Detach: create a new bare Location at this pin's coordinates so
            # the pin retains its own independent community wiki page.
            lat = float(pin.effective_latitude or 0)
            lng = float(pin.effective_longitude or 0)
            name = (pin.location.name if pin.location else None) or pin.nickname or "Unnamed Location"
            location = Location.objects.create(
                name=name,
                latitude=lat,
                longitude=lng,
            )

        pin.location = location
        pin.save(update_fields=["location"])
        pin.refresh_from_db()

        return render(request, "dashboard/partials/pin_overview_partial.html", _overview_context(pin))
