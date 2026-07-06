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
from urbanlens.dashboard.services.apis.locations.base import default_bbox
from urbanlens.dashboard.services.locations.boundaries import boundary_as_multipolygon
from urbanlens.dashboard.services.timeout_utils import call_with_deadline

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
            defaults={"location": pin.location, "profile": profile},
        )
        # Sync location in case pin.location was reassigned since this campus was created.
        if campus.location_id != pin.location_id:
            campus.location = pin.location
            campus.save(update_fields=["location", "updated"])

        if campus.generated_polygon is None:
            # Bounded to a hard wall-clock deadline: the BoundaryProviderChain can
            # fall through several providers (Microsoft/Google building footprints
            # allow up to 180s each), and requests' own timeout= only bounds
            # inactivity between reads, not total call duration -- without this, a
            # slow/down provider (e.g. Overpass 504ing) can hold the gevent worker
            # hostage for the whole chain, stalling every other request on it.
            campus.generated_polygon = call_with_deadline(
                lambda: boundary_as_multipolygon(lat, lon, name=pin.effective_name),
                timeout=20,
                default=MultiPolygon(default_bbox(lat, lon), srid=4326),
            )
            campus.save(update_fields=["generated_polygon", "updated"])

        effective = campus.polygon or campus.generated_polygon
        return JsonResponse(
            {
                "polygon": json.loads(effective.geojson) if effective else None,
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

        # Auto-create a Location for legacy pins that don't have one yet.
        if not pin.location_id:
            lat = pin.effective_latitude
            lon = pin.effective_longitude
            if not lat or not lon:
                return JsonResponse({"error": "Pin has no coordinates"}, status=400)
            location = Location.objects.get_for_point(lat, lon)
            if not location:
                location = Location.objects.create(
                    name=pin.effective_name or "Unnamed Location",
                    latitude=lat,
                    longitude=lon,
                )
            pin.location = location
            pin.save(update_fields=["location"])

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
            defaults={"location": pin.location, "profile": profile},
        )
        # Sync stale location reference if pin.location was reassigned.
        if campus.location_id != pin.location_id:
            campus.location = pin.location

        if polygon_geojson:
            geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
            if isinstance(geom, Polygon):
                geom = MultiPolygon(geom, srid=geom.srid)
            campus.polygon = geom
        else:
            # Clear user drawing; ensure generated_polygon is cached so the
            # fallback display doesn't require a fresh API call.
            campus.polygon = None
            if campus.generated_polygon is None:
                lat = pin.effective_latitude
                lon = pin.effective_longitude
                if lat is None or lon is None:
                    return JsonResponse({"error": "Pin has no coordinates"}, status=400)
                # Bounded to a hard wall-clock deadline: the BoundaryProviderChain can
                # fall through several providers (Microsoft/Google building footprints
                # allow up to 180s each), and requests' own timeout= only bounds
                # inactivity between reads, not total call duration -- without this, a
                # slow/down provider (e.g. Overpass 504ing) can hold the gevent worker
                # hostage for the whole chain, stalling every other request on it.
                campus.generated_polygon = call_with_deadline(
                    lambda: boundary_as_multipolygon(lat, lon, name=pin.effective_name),
                    timeout=20,
                    default=MultiPolygon(default_bbox(lat, lon), srid=4326),
                )

        campus.save(update_fields=["polygon", "generated_polygon", "location", "updated"])

        effective = campus.polygon or campus.generated_polygon
        return JsonResponse({"status": "ok", "polygon": json.loads(effective.geojson) if effective else None})

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
                Campus.objects.filter(profile=profile, pin__isnull=False).select_related("location"),
            )
            covered_location_ids = {c.location_id for c in pin_campuses}
            location_defaults = list(
                Campus.objects.filter(profile__isnull=True, pin__isnull=True).exclude(location_id__in=covered_location_ids).select_related("location"),
            )
            campuses = pin_campuses + location_defaults
        else:
            campuses = list(
                Campus.objects.filter(profile__isnull=True, pin__isnull=True).select_related("location"),
            )

        result = []
        for c in campuses:
            effective = c.polygon or c.generated_polygon
            result.append(
                {
                    "id": c.id,
                    "location_id": c.location_id,
                    "latitude": float(c.location.latitude),
                    "longitude": float(c.location.longitude),
                    "polygon": json.loads(effective.geojson) if effective else None,
                    "default_radius_meters": c.default_radius_meters,
                },
            )
        return JsonResponse({"campuses": result})
