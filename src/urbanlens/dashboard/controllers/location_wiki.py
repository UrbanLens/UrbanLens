"""Location wiki controller - community-editable page for a shared Location."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.location.edit_model import LocationEdit
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

# Fields a community member may edit via "Suggest edits".
_WIKI_EDITABLE_FIELDS = ("name", "description", "latitude", "longitude")


class LocationWikiView(LoginRequiredMixin, View):
    """Main wiki page for a Location.

    GET  /location/<id>/wiki/  → full wiki page
    """

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        # Only count root pins (not detail pins), and count distinct users.
        root_pins = location.pins.filter(parent_pin__isnull=True, parent_location__isnull=True)
        pin_count = root_pins.values("profile").distinct().count()
        first_pinned = root_pins.select_related("profile__user").order_by("created").first()

        # The requesting user's own pin for this location (used for the back-link).
        user_pin = location.pins.filter(profile=profile).first()

        from urbanlens.dashboard.models.pin.model import PinType
        from urbanlens.dashboard.models.tags.model import COLOR_CHOICES

        detail_pin_icon_choices = [
            ("place", "Place"), ("business", "Building"), ("door_front", "Entrance"),
            ("star", "Star"), ("warning", "Warning"), ("info", "Info"),
            ("camera_alt", "Camera"), ("local_parking", "Parking"),
            ("stairs", "Stairs"), ("elevator", "Elevator"),
            ("exit_to_app", "Exit"), ("lock", "Lock"),
            ("construction", "Construction"), ("emergency", "Emergency"),
        ]

        return render(
            request,
            "dashboard/pages/location/wiki.html",
            {
                "location": location,
                "pin_count": pin_count,
                "first_pinned": first_pinned,
                "user_pin": user_pin,
                "page_name": "location-wiki",
                "pin_type_choices": PinType.choices,
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
            },
        )


class LocationWikiEditView(LoginRequiredMixin, View):
    """Suggest (and immediately apply) a community edit to a Location's wiki fields.

    POST /location/<id>/wiki/edit/
    Body (JSON or form): field=value pairs for any subset of _WIKI_EDITABLE_FIELDS.
    Records a LocationEdit and applies changes to the Location.
    """

    def post(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        changes: dict[str, dict] = {}
        for field in _WIKI_EDITABLE_FIELDS:
            if field not in body:
                continue
            new_val = body[field]
            old_val = getattr(location, field, None)
            if str(new_val) == str(old_val):
                continue
            # Cast numeric fields.
            if field in {"latitude", "longitude"}:
                try:
                    new_val = float(new_val)
                except (TypeError, ValueError):
                    continue
            changes[field] = {"from": old_val, "to": new_val}

        if not changes:
            return JsonResponse({"ok": True, "message": "No changes detected."})

        # Apply changes.
        for field, diff in changes.items():
            setattr(location, field, diff["to"])
        location.save()

        LocationEdit.objects.create(
            location=location,
            editor=profile,
            changes=changes,
        )

        return JsonResponse({"ok": True, "changes": list(changes.keys())})


class LocationWikiBboxView(LoginRequiredMixin, View):
    """Update the bounding box polygon for a Location.

    GET  /location/<id>/wiki/bbox/  → returns current bbox as GeoJSON
    POST /location/<id>/wiki/bbox/  → { "polygon": <GeoJSON geometry> }
    """

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        if location.bounding_box:
            import json as _json

            return JsonResponse({"polygon": _json.loads(location.bounding_box.geojson)})
        return JsonResponse({"polygon": None})

    def post(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body)
            polygon_geojson = body.get("polygon")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        if not polygon_geojson:
            return JsonResponse({"error": "polygon is required"}, status=400)

        try:
            from django.contrib.gis.geos import GEOSGeometry

            geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
        except Exception as exc:
            logger.exception("Invalid polygon GeoJSON: %s", exc)
            return JsonResponse({"error": "Invalid polygon geometry"}, status=400)

        old_wkt = location.bounding_box.wkt if location.bounding_box else None
        location.bounding_box = geom
        location.save(update_fields=["bounding_box", "updated"])

        LocationEdit.objects.create(
            location=location,
            editor=profile,
            changes={"bounding_box": {"from": old_wkt, "to": geom.wkt}},
        )

        return JsonResponse({"ok": True})


class LocationWikiHistoryView(LoginRequiredMixin, View):
    """HTMX partial: edit history list for a Location.

    GET /location/<id>/wiki/history/
    """

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        edits = location.edits.select_related("editor__user", "reverted_by").order_by("-created")
        return render(
            request,
            "dashboard/pages/location/wiki_history.html",
            {"location": location, "edits": edits},
        )


class LocationWikiRevertView(LoginRequiredMixin, View):
    """Revert a specific LocationEdit.

    POST /location/<id>/wiki/history/<edit_id>/revert/
    Creates a new LocationEdit that restores the "from" values and marks the
    original edit as reverted.
    """

    def post(self, request, location_uuid, edit_id: int):
        location = get_object_or_404(Location, uuid=location_uuid)
        target_edit = get_object_or_404(LocationEdit, id=edit_id, location=location)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if target_edit.reverted:
            return JsonResponse({"error": "This edit has already been reverted."}, status=400)

        revert_changes: dict[str, dict] = {}
        for field, diff in target_edit.changes.items():
            old_val = diff.get("from")
            current_val = (
                getattr(location, field, None)
                if field != "bounding_box"
                else (location.bounding_box.wkt if location.bounding_box else None)
            )
            revert_changes[field] = {"from": current_val, "to": old_val}
            if field == "bounding_box":
                if old_val:
                    from django.contrib.gis.geos import GEOSGeometry

                    location.bounding_box = GEOSGeometry(old_val, srid=4326)
                else:
                    location.bounding_box = None
            else:
                setattr(location, field, old_val)

        location.save()

        revert_edit = LocationEdit.objects.create(
            location=location,
            editor=profile,
            changes=revert_changes,
        )
        target_edit.reverted = True
        target_edit.reverted_by = revert_edit
        target_edit.save(update_fields=["reverted", "reverted_by", "updated"])

        return JsonResponse({"ok": True})
