"""Wiki controller - community-editable page for a shared place.

Routes are keyed by the Location slug (the stable URL token) but every view
operates on the :class:`~urbanlens.dashboard.models.wiki.model.Wiki` for that
Location, materialising one lazily via
``Wiki.objects.get_or_create_for_location`` on first access.
"""

from __future__ import annotations

import contextlib
import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Polygon
from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.locations.boundaries import boundary_as_multipolygon

logger = logging.getLogger(__name__)

_WIKI_SECURITY_FIELDS = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

# Fields a community member may edit via "Suggest edits". name/description/
# security/dates live on the Wiki; latitude/longitude repoint the Wiki to a
# different (find-or-created) Location rather than mutating a shared row.
_WIKI_EDITABLE_FIELDS = ("name", "description", "latitude", "longitude", *_WIKI_SECURITY_FIELDS, "date_abandoned", "date_last_active")


def _resolve_wiki(location_slug: str) -> tuple[Location, Wiki]:
    """Resolve the Location for a slug and its (lazily-created) Wiki."""
    location = get_object_or_404(Location, slug=location_slug)
    wiki, _created = Wiki.objects.get_or_create_for_location(location)
    return location, wiki


def _apply_coordinate_change(wiki: Wiki, coord_vals: dict[str, float]) -> str | None:
    """Repoint the wiki to a find-or-created Location for the new coordinates.

    Returns an error message string when the change can't be applied (e.g. the
    target Location already hosts a different wiki), else None.
    """
    current = wiki.location
    latitude = coord_vals.get("latitude", float(current.latitude) if current else None)
    longitude = coord_vals.get("longitude", float(current.longitude) if current else None)
    if latitude is None or longitude is None:
        return "Both latitude and longitude are required."

    target, _created = Location.objects.get_nearby_or_create(
        latitude,
        longitude,
        defaults={"official_name": current.official_name if current else None},
    )
    if target.pk == (current.pk if current else None):
        return None
    # OneToOne: a Location can host only one wiki.
    try:
        existing = target.wiki
    except ObjectDoesNotExist:
        existing = None
    if existing is not None and existing.pk != wiki.pk:
        return "Another community page already exists at those coordinates."
    wiki.location = target
    return None


class LocationWikiView(LoginRequiredMixin, View):
    """Main wiki page for a place.

    GET  /location/<slug>/wiki/  → full wiki page
    """

    def get(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        # Only count root pins (not detail pins), and count distinct users.
        root_pins = location.pins.filter(parent_pin__isnull=True, parent_wiki__isnull=True)
        pin_count = root_pins.values("profile").distinct().count()
        first_pinned = root_pins.select_related("profile__user").order_by("created").first()

        # The requesting user's own pin for this location (used for the back-link).
        user_pin = location.pins.filter(profile=profile).first()

        # Other Locations whose bounding box also covers the user's pin point.
        # These are potential alternative associations the user may prefer.
        if user_pin:
            lat = user_pin.effective_latitude
            lng = user_pin.effective_longitude
            other_locations = Location.objects.within_bounding_box(float(lat), float(lng)).exclude(pk=location.pk).order_by("official_name") if lat is not None and lng is not None else Location.objects.none()
        else:
            other_locations = Location.objects.none()

        from urbanlens.dashboard.models.badges.model import COLOR_CHOICES
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

        return render(
            request,
            "dashboard/pages/location/wiki.html",
            {
                "wiki": wiki,
                "location": location,
                "pin_count": pin_count,
                "first_pinned": first_pinned,
                "user_pin": user_pin,
                "other_locations": other_locations,
                "page_name": "location-wiki",
                "pin_type_choices": PinType.choices,
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "markup_fill_color": profile.markup_fill_color,
                "markup_fill_opacity": profile.markup_fill_opacity,
                "markup_border_color": profile.markup_border_color,
                "markup_border_opacity": profile.markup_border_opacity,
                "security_level_choices": SecurityLevel.choices,
                "location_security_values": [
                    ("fences", "Fences", wiki.fences),
                    ("alarms", "Alarms", wiki.alarms),
                    ("cameras", "Cameras", wiki.cameras),
                    ("security", "Security", wiki.security),
                    ("signs", "Signs", wiki.signs),
                    ("vps", "VPS", wiki.vps),
                    ("plywood", "Plywood", wiki.plywood),
                    ("locked", "Locked", wiki.locked),
                ],
            },
        )


class LocationWikiEditView(LoginRequiredMixin, View):
    """Suggest (and immediately apply) a community edit to a Wiki's fields.

    POST /location/<slug>/wiki/edit/
    Body (JSON or form): field=value pairs for any subset of _WIKI_EDITABLE_FIELDS.
    Records a WikiEdit and applies changes to the Wiki. A coordinate edit
    find-or-creates a Location for the new point and repoints ``wiki.location``.
    """

    def post(self, request, location_slug):
        _location, wiki = _resolve_wiki(location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        from datetime import datetime

        valid_security = {v for v, _ in SecurityLevel.choices}
        # new_vals holds the actual Python values to set on the wiki.
        # coord_vals holds a pending latitude/longitude change (repoints Location).
        # changes holds JSON-safe strings for the WikiEdit audit record.
        new_vals: dict[str, object] = {}
        coord_vals: dict[str, float] = {}
        changes: dict[str, dict] = {}
        for field in _WIKI_EDITABLE_FIELDS:
            if field not in body:
                continue
            raw = body[field]
            old_val = getattr(wiki, field, None)
            if str(raw) == str(old_val):
                continue
            if field in {"latitude", "longitude"}:
                try:
                    coord_vals[field] = float(raw)
                except (TypeError, ValueError):
                    continue
                changes[field] = {"from": str(old_val), "to": str(coord_vals[field])}
                continue
            if field in _WIKI_SECURITY_FIELDS:
                if raw not in valid_security:
                    continue
                new_val: object = raw
            elif field in {"date_abandoned", "date_last_active"}:
                if not raw:
                    new_val = None
                else:
                    try:
                        new_val = datetime.strptime(raw, "%Y-%m-%d").date()
                    except ValueError:
                        continue
            else:
                new_val = raw
            new_vals[field] = new_val
            changes[field] = {"from": str(old_val), "to": str(new_val)}

        # Apply a coordinate change by repointing to a find-or-created Location.
        if coord_vals:
            error = _apply_coordinate_change(wiki, coord_vals)
            if error is not None:
                return JsonResponse({"error": error}, status=400)

        if not changes:
            return JsonResponse({"ok": True, "message": "No changes detected."})

        # Apply wiki-field changes.
        for field, val in new_vals.items():
            setattr(wiki, field, val)
        wiki.save()

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes=changes,
        )

        return JsonResponse({"ok": True, "changes": list(changes.keys())})


class LocationWikiBboxView(LoginRequiredMixin, View):
    """Update the bounding box polygon for a place.

    GET  /location/<slug>/wiki/bbox/  → returns current bbox as GeoJSON
    POST /location/<slug>/wiki/bbox/  → { "polygon": <GeoJSON geometry> }

    The bounding box lives on the Location's default Campus (a physical-place
    attribute); the wiki simply provides the editing UI.
    """

    def get(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        campus, _ = Campus.objects.get_or_create_default_for_wiki(wiki, location=location)
        if campus.polygon is None:
            campus.polygon = boundary_as_multipolygon(float(location.latitude), float(location.longitude), name=wiki.name)
            campus.save(update_fields=["polygon", "updated"])
        return JsonResponse({"polygon": json.loads(campus.polygon.geojson) if campus.polygon else None})

    def post(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body)
            polygon_geojson = body.get("polygon")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        if not polygon_geojson:
            geom = boundary_as_multipolygon(float(location.latitude), float(location.longitude), name=wiki.name)
        else:
            try:
                geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
                if isinstance(geom, Polygon):
                    geom = MultiPolygon(geom, srid=geom.srid)
            except (GEOSException, ValueError) as exc:
                logger.exception("Invalid polygon GeoJSON: %s", exc)
                return JsonResponse({"error": "Invalid polygon geometry"}, status=400)

        # Check area against the site-wide limit.  Project to an equal-area CRS
        # (EPSG:6933) so the area calculation is meaningful globally.
        from urbanlens.dashboard.models.site_settings import SiteSettings

        max_km2 = SiteSettings.get_current().max_bbox_area_km2
        try:
            area_km2 = geom.transform(6933, clone=True).area / 1_000_000
        except GEOSException:
            area_km2 = 0.0
        if area_km2 > max_km2:
            return JsonResponse(
                {
                    "error": f"Bounding box is too large ({area_km2:,.0f} km²). Maximum allowed area is {max_km2:,.0f} km².",
                },
                status=400,
            )

        campus, _ = Campus.objects.get_or_create_default_for_wiki(wiki, location=location)
        old_wkt = campus.polygon.wkt if campus.polygon else None
        campus.polygon = geom
        campus.save(update_fields=["polygon", "updated"])

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"bounding_box": {"from": old_wkt, "to": geom.wkt}},
        )

        return JsonResponse({"ok": True, "polygon": json.loads(campus.polygon.geojson) if campus.polygon else None})


class LocationWikiHistoryView(LoginRequiredMixin, View):
    """HTMX partial: edit history list for a wiki.

    GET /location/<slug>/wiki/history/
    """

    def get(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        edits = wiki.edits.select_related("editor__user", "reverted_by").order_by("-created")
        return render(
            request,
            "dashboard/pages/location/wiki_history.html",
            {"location": location, "wiki": wiki, "edits": edits},
        )


class LocationWikiRevertView(LoginRequiredMixin, View):
    """Revert a specific WikiEdit.

    POST /location/<slug>/wiki/history/<edit_id>/revert/
    Creates a new WikiEdit that restores the "from" values and marks the
    original edit as reverted.
    """

    def post(self, request, location_slug, edit_id: int):
        location, wiki = _resolve_wiki(location_slug)
        target_edit = get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if target_edit.reverted:
            return JsonResponse({"error": "This edit has already been reverted."}, status=400)

        revert_changes: dict[str, dict] = {}
        for field, diff in target_edit.changes.items():
            old_val = diff.get("from")
            if field == "bounding_box":
                campus, _ = Campus.objects.get_or_create_default_for_wiki(wiki, location=location)
                current_val = campus.polygon.wkt if campus.polygon else None
                revert_changes[field] = {"from": current_val, "to": old_val}
                if old_val:
                    restored = GEOSGeometry(old_val, srid=4326)
                    if isinstance(restored, Polygon):
                        restored = MultiPolygon(restored, srid=restored.srid)
                    campus.polygon = restored
                else:
                    campus.polygon = None
                campus.save(update_fields=["polygon", "updated"])
            elif field in {"latitude", "longitude"}:
                # Coordinate reverts repoint the Location; skip silently if the
                # target place is unavailable. Handled together below.
                current_val = getattr(wiki, field, None)
                revert_changes[field] = {"from": str(current_val), "to": old_val}
                with contextlib.suppress(TypeError, ValueError):
                    _apply_coordinate_change(wiki, {field: float(old_val)})
            else:
                current_val = getattr(wiki, field, None)
                revert_changes[field] = {"from": current_val, "to": old_val}
                setattr(wiki, field, old_val)

        wiki.save()

        revert_edit = WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes=revert_changes,
        )
        target_edit.reverted = True
        target_edit.reverted_by = revert_edit
        target_edit.save(update_fields=["reverted", "reverted_by", "updated"])

        return JsonResponse({"ok": True})
