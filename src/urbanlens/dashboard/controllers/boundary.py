"""Boundary controller - get, save, and list typed property/building boundaries.

Endpoints are JSON-only (no template rendering). The boundary editor UI is
rendered by the pin detail and wiki page templates; these views serve its data
calls. Both endpoints share one payload shape:

    {
        "latitude": ..., "longitude": ...,
        "default_radius_meters": 50,
        "pending": bool,          # default-boundary generation in flight
        "boundaries": {
            "property": {"polygon": <GeoJSON|null>, "source": "pin|wiki|inherited|generated|circle|null"},
            "building": {"polygon": <GeoJSON|null>, "source": ...},
        },
        "detail_buildings": [{"pin_id": ..., "polygon": <GeoJSON>}, ...],
    }

POST bodies carry ``{"boundary_type": "property"|"building", "polygon":
<GeoJSON geometry|null>}``; null clears the custom drawing so display falls
back down the resolution chain.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Polygon
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.boundary.queryset import DEFAULT_RADIUS_METERS
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.external_data import schedule_panel_fetch
from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, schedule_location_boundary_generation
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


def _geojson(geom) -> dict | None:
    """Serialize a GEOS geometry to a GeoJSON dict, or None."""
    return json.loads(geom.geojson) if geom else None


def _parse_multipolygon(polygon_geojson: dict) -> MultiPolygon:
    """Parse a GeoJSON geometry into a MultiPolygon.

    Args:
        polygon_geojson: A GeoJSON Polygon or MultiPolygon geometry dict.

    Returns:
        The parsed geometry, coerced to MultiPolygon.

    Raises:
        ValueError: If the payload isn't valid polygonal GeoJSON.
        TypeError: If the geometry is valid but not polygonal.
    """
    try:
        geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
    except (GEOSException, TypeError, ValueError) as exc:
        raise ValueError("Invalid polygon geometry") from exc
    if isinstance(geom, Polygon):
        geom = MultiPolygon(geom, srid=geom.srid)
    if not isinstance(geom, MultiPolygon):
        raise TypeError("Boundary must be a Polygon or MultiPolygon")
    return geom


def _parse_boundary_type(value) -> str | None:
    """Return the validated boundary type value, or None when invalid."""
    return value if value in BoundaryType.values else None


def _detail_building_entries(pin: Pin) -> list[dict]:
    """Building boundaries drawn on this pin's detail pins (display-only).

    The map hides the building layer only when neither the pin itself nor any
    of its detail pins has a building boundary.

    Args:
        pin: The parent pin whose detail pins are inspected.

    Returns:
        List of ``{"pin_id", "polygon"}`` dicts.
    """
    rows = Boundary.objects.filter(pin__parent_pin=pin, boundary_type=BoundaryType.BUILDING).select_related("pin")
    entries = []
    for row in rows:
        polygon = row.drawn_or_generated_polygon
        if polygon is not None:
            entries.append({"pin_id": row.pin_id, "polygon": _geojson(polygon)})
    return entries


def _pin_boundary_payload(pin: Pin, *, pending: bool) -> dict:
    """Full boundary payload for a pin detail page map."""
    boundaries = {}
    for boundary_type in (BoundaryType.PROPERTY, BoundaryType.BUILDING):
        polygon, source = Boundary.objects.resolve_for_pin(pin, boundary_type)
        boundaries[str(boundary_type.value)] = {"polygon": _geojson(polygon), "source": source}
    return {
        "latitude": pin.effective_latitude,
        "longitude": pin.effective_longitude,
        "default_radius_meters": DEFAULT_RADIUS_METERS,
        "pending": pending,
        "boundaries": boundaries,
        "detail_buildings": _detail_building_entries(pin),
    }


def _wiki_boundary_payload(wiki: Wiki, *, pending: bool) -> dict:
    """Full boundary payload for a wiki page map."""
    boundaries = {}
    for boundary_type in (BoundaryType.PROPERTY, BoundaryType.BUILDING):
        polygon, source = Boundary.objects.resolve_for_wiki(wiki, boundary_type)
        boundaries[str(boundary_type.value)] = {"polygon": _geojson(polygon), "source": source}
    location = wiki.location
    return {
        "latitude": float(location.latitude) if location and location.latitude is not None else None,
        "longitude": float(location.longitude) if location and location.longitude is not None else None,
        "default_radius_meters": DEFAULT_RADIUS_METERS,
        "pending": pending,
        "boundaries": boundaries,
        "detail_buildings": [],
    }


class BoundaryController(LoginRequiredMixin, GenericViewSet):
    """API endpoints for a pin's typed boundary data."""

    def get_boundaries(self, request: HttpRequest, pin_slug):
        """Return the effective property/building boundaries for a pin.

        Default boundaries are generated lazily: the first view of a pin
        detail page schedules the provider chain in Celery (via the
        "boundary" panel source) and the map JS polls while ``pending``.
        """
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            pin = Pin.objects.select_related("location", "wiki", "parent_pin").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        pending = False
        if pin.location_id and not boundary_generation_ran(pin.location):
            # Heavy provider-chain work never runs on the request path.
            pending = schedule_panel_fetch("boundary", pin)

        return JsonResponse(_pin_boundary_payload(pin, pending=pending))

    def save_boundary(self, request: Request, pin_slug):
        """Create, update, or clear the user's custom boundary of one type.

        Sending ``polygon: null`` deletes the pin's custom row so display
        falls back down the resolution chain (parent pin, wiki, generated,
        circle). Pin rows only ever hold user-drawn geometry - generated
        polygons live on the shared location-default rows.
        """
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            pin = Pin.objects.select_related("location", "wiki", "parent_pin").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        data = request.data
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid request body"}, status=400)
        boundary_type = _parse_boundary_type(data.get("boundary_type"))
        if boundary_type is None:
            return JsonResponse({"error": "boundary_type must be 'property' or 'building'"}, status=400)

        try:
            profile: Profile = request.user.profile
        except Profile.DoesNotExist:
            return JsonResponse({"error": "User has no profile"}, status=403)

        polygon_geojson = data.get("polygon")
        if polygon_geojson:
            try:
                geom = _parse_multipolygon(polygon_geojson)
            except (TypeError, ValueError) as exc:
                return JsonResponse({"error": str(exc)}, status=400)
            row, _created = Boundary.objects.get_or_create(
                pin=pin,
                boundary_type=boundary_type,
                defaults={"profile": profile, "location": pin.location},
            )
            row.polygon = geom
            if row.location_id != pin.location_id:
                row.location = pin.location
            row.save(update_fields=["polygon", "location", "updated"])
        else:
            # Clearing removes the custom row entirely - fall back down the chain.
            Boundary.objects.filter(pin=pin, boundary_type=boundary_type).delete()

        pending = False
        if pin.location_id and not boundary_generation_ran(pin.location):
            pending = schedule_panel_fetch("boundary", pin)

        payload = _pin_boundary_payload(pin, pending=pending)
        payload["status"] = "ok"
        return JsonResponse(payload)

    def list_boundaries(self, request: HttpRequest):
        """Return all boundaries visible to the current user for the main map overlay.

        Returns the user's own pin boundaries, plus location-default rows for
        locations not already covered by one of those pin boundaries.
        """
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        try:
            profile: Profile | None = request.user.profile
        except Profile.DoesNotExist:
            profile = None

        if profile:
            pin_rows = list(Boundary.objects.for_profile(profile).with_coordinate_location())
            covered = {(row.pin.location_id, row.boundary_type) for row in pin_rows if row.pin and row.pin.location_id}
            defaults = [row for row in Boundary.objects.location_defaults().with_coordinate_location() if (row.location_id, row.boundary_type) not in covered]
            rows = pin_rows + defaults
        else:
            rows = list(Boundary.objects.location_defaults().with_coordinate_location())

        result = []
        for row in rows:
            location = row.coordinate_location
            if location is None or location.latitude is None or location.longitude is None:
                continue
            polygon = row.effective_polygon
            result.append(
                {
                    "id": row.id,
                    "boundary_type": row.boundary_type,
                    "location_id": location.id,
                    "latitude": float(location.latitude),
                    "longitude": float(location.longitude),
                    "polygon": _geojson(polygon),
                    "default_radius_meters": row.default_radius_meters,
                },
            )
        return JsonResponse({"boundaries": result})


class WikiBoundaryView(LoginRequiredMixin, View):
    """Community boundary editor endpoints for the wiki page.

    GET  /location/<slug>/wiki/boundary/  → typed boundary payload
    POST /location/<slug>/wiki/boundary/  → {"boundary_type": ..., "polygon": <GeoJSON|null>}

    Community drawings are stored on wiki-keyed Boundary rows; the shared
    location-default rows only ever hold API-generated geometry, so a
    community edit can never influence point→location matching.
    """

    def get(self, request, location_slug):
        """Return the wiki's effective boundaries, scheduling generation when needed."""
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        pending = schedule_location_boundary_generation(location, profile)
        return JsonResponse(_wiki_boundary_payload(wiki, pending=pending))

    def post(self, request, location_slug):
        """Save or clear the community-drawn boundary of one type, with audit."""
        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError, AttributeError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return JsonResponse({"error": "Invalid request body"}, status=400)
        boundary_type = _parse_boundary_type(body.get("boundary_type"))
        if boundary_type is None:
            return JsonResponse({"error": "boundary_type must be 'property' or 'building'"}, status=400)

        polygon_geojson = body.get("polygon")
        row = Boundary.objects.row_for_wiki(wiki, boundary_type)
        old_wkt = row.polygon.wkt if row and row.polygon else None

        if polygon_geojson:
            try:
                geom = _parse_multipolygon(polygon_geojson)
            except (TypeError, ValueError) as exc:
                return JsonResponse({"error": str(exc)}, status=400)

            # Check area against the site-wide limit.  Project to an equal-area
            # CRS (EPSG:6933) so the area calculation is meaningful globally.
            from urbanlens.dashboard.models.site_settings import SiteSettings

            max_km2 = SiteSettings.get_current().max_bbox_area_km2
            try:
                area_km2 = geom.transform(6933, clone=True).area / 1_000_000
            except GEOSException:
                area_km2 = 0.0
            if area_km2 > max_km2:
                return JsonResponse(
                    {"error": f"Boundary is too large ({area_km2:,.0f} km²). Maximum allowed area is {max_km2:,.0f} km²."},
                    status=400,
                )

            if row is None:
                row = Boundary(wiki=wiki, location=location, boundary_type=boundary_type)
            row.polygon = geom
            if row.location_id != wiki.location_id:
                row.location = wiki.location
            row.save()
            new_wkt = geom.wkt
        else:
            if row is not None:
                row.delete()
            new_wkt = None

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={f"boundary_{boundary_type}": {"from": old_wkt, "to": new_wkt}},
        )

        pending = schedule_location_boundary_generation(location, profile)
        payload = _wiki_boundary_payload(wiki, pending=pending)
        payload["ok"] = True
        return JsonResponse(payload)
