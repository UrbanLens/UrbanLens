"""Campus boundary controller - get, save, and list campus regions."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
from django.http import HttpRequest, JsonResponse
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.external_data import schedule_panel_fetch

if TYPE_CHECKING:
    from rest_framework.request import Request

logger = logging.getLogger(__name__)


class CampusController(LoginRequiredMixin, GenericViewSet):
    """API endpoints for Campus boundary data.

    Endpoints are JSON-only (no template rendering).  The campus editor UI
    is rendered by the pin detail template; these views serve its data calls.
    """

    def get_campus(self, request: HttpRequest, pin_slug):
        """Return this user's effective pin-detail campus boundary.

        Pin boundaries are stored as pin-scoped Campus rows (pin=<pin>).
        Location/wiki boundaries use profile=None, pin=None rows, so the two
        boundary types stay strictly separate.

        On first access the boundary is auto-generated from the BoundaryProviderChain
        and cached in generated_polygon.  Subsequent loads hit the DB only.
        """
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        lat = pin.effective_latitude
        lon = pin.effective_longitude
        if lat is None or lon is None or not pin.location_id:
            return JsonResponse({"polygon": None, "default_radius_meters": 50, "latitude": lat, "longitude": lon})

        try:
            profile: Profile = request.user.profile
        except Profile.DoesNotExist:
            return JsonResponse({"error": "User has no profile"}, status=403)

        campus, _ = Campus.objects.get_or_create(
            pin=pin,
            defaults={"profile": profile},
        )
        if campus.wiki_id != pin.wiki_id or campus.location_id != pin.location_id:
            campus.wiki = pin.wiki
            campus.location = pin.location
            campus.save(update_fields=["wiki", "location", "updated"])

        # Boundary generation (Overpass, building-footprint downloads, shapely
        # work) never runs on the request path: schedule it in Celery and tell
        # the map JS to poll. A user-drawn polygon still renders immediately --
        # generation only fills the fallback shown when nothing is drawn.
        pending = False
        if campus.generated_polygon is None:
            pending = schedule_panel_fetch("campus", pin)

        effective = campus.polygon or campus.generated_polygon
        return JsonResponse(
            {
                "polygon": json.loads(effective.geojson) if effective else None,
                "pending": pending and effective is None,
                "default_radius_meters": campus.default_radius_meters,
                "latitude": lat,
                "longitude": lon,
            },
        )

    def save_campus(self, request: Request, pin_slug):
        """Create or update the current user's pin boundary.

        Sending polygon=null clears the user-drawn polygon and resets to the
        cached generated_polygon (generating it first if not yet cached).
        Sending a GeoJSON polygon stores it as the user-drawn custom boundary.
        """
        from urbanlens.dashboard.models.location.model import Location

        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        data = request.data
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid request body"}, status=400)

        try:
            profile: Profile = request.user.profile
        except Profile.DoesNotExist:
            return JsonResponse({"error": "User has no profile"}, status=403)

        polygon_geojson = data.get("polygon")
        campus, _ = Campus.objects.get_or_create(
            pin=pin,
            defaults={"profile": profile},
        )
        # Sync stale location reference if pin.location was reassigned. (TODO Look at this. Should campus even have a location field?)
        if campus.location_id != pin.location_id:
            campus.location = pin.location

        if polygon_geojson:
            geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
            if isinstance(geom, Polygon):
                geom = MultiPolygon(geom, srid=geom.srid)
            campus.polygon = geom
        else:
            # Clear user drawing; kick off default-boundary generation in the
            # background so the map has a fallback to show. The response marks
            # itself pending and the map JS polls get_campus until the task
            # lands the generated polygon.
            campus.polygon = None
            if campus.generated_polygon is None:
                lat = pin.effective_latitude
                lon = pin.effective_longitude
                if lat is None or lon is None:
                    return JsonResponse({"error": "Pin has no coordinates"}, status=400)
                schedule_panel_fetch("campus", pin)

        # generated_polygon is deliberately absent: only the background fetch
        # task writes it (single-column queryset update), so this save can
        # never clobber a boundary the worker landed concurrently.
        campus.save(update_fields=["polygon", "wiki", "location", "updated"])

        effective = campus.polygon or campus.generated_polygon
        return JsonResponse(
            {
                "status": "ok",
                "polygon": json.loads(effective.geojson) if effective else None,
                "pending": effective is None,
            },
        )

    def list_campuses(self, request: HttpRequest):
        """Return all campus boundaries visible to the current user for the main map overlay.

        Returns pin-scoped campuses for the user's own pins, plus location-default
        campuses for locations not already covered by a pin campus.
        """
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        try:
            profile: Profile | None = request.user.profile
        except Profile.DoesNotExist:
            profile = None

        if profile:
            pin_campuses = list(
                Campus.objects.filter(profile=profile, pin__isnull=False).with_coordinate_location(),
            )
            covered_wiki_ids = {c.wiki_id for c in pin_campuses if c.wiki_id}
            location_defaults = list(
                Campus.objects.filter(profile__isnull=True, pin__isnull=True).exclude(wiki_id__in=covered_wiki_ids).with_coordinate_location(),
            )
            campuses = pin_campuses + location_defaults
        else:
            campuses = list(
                Campus.objects.filter(profile__isnull=True, pin__isnull=True).with_coordinate_location(),
            )

        result = []
        for c in campuses:
            effective = c.polygon or c.generated_polygon
            location = c.coordinate_location
            if location is None or location.latitude is None or location.longitude is None:
                continue
            result.append(
                {
                    "id": c.id,
                    "location_id": location.id,
                    "latitude": float(location.latitude),
                    "longitude": float(location.longitude),
                    "polygon": json.loads(effective.geojson) if effective else None,
                    "default_radius_meters": c.default_radius_meters,
                },
            )
        return JsonResponse({"campuses": result})
