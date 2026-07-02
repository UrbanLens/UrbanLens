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
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

if TYPE_CHECKING:
    from rest_framework.request import Request

logger = logging.getLogger(__name__)


def _default_campus_polygon(latitude: float, longitude: float, *, name: str | None = None) -> MultiPolygon:
    """Resolve the default boundary and normalize it for Campus.polygon."""
    geom = BoundaryProviderChain().boundary_for_point(float(latitude), float(longitude), name=name)
    return MultiPolygon(geom, srid=geom.srid) if isinstance(geom, Polygon) else geom


class CampusController(LoginRequiredMixin, GenericViewSet):
    """API endpoints for Campus boundary data.

    Endpoints are JSON-only (no template rendering).  The campus editor UI
    is rendered by the pin detail template; these views serve its data calls.
    """

    def get_campus(self, request: HttpRequest, pin_slug):
        """Return this user's effective pin-detail campus boundary.

        Pin detail boundaries are stored as user-scoped Campus rows
        (profile=<request.user.profile>). Location/wiki boundaries use the same
        Campus model with profile=None, so the two boundaries stay separate.
        """
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        lat = float(pin.location.latitude) if pin.location_id else pin.effective_latitude
        lon = float(pin.location.longitude) if pin.location_id else pin.effective_longitude
        if lat is None or lon is None:
            return JsonResponse({"polygon": None, "default_radius_meters": 50, "latitude": lat, "longitude": lon})
        if not pin.location_id:
            return JsonResponse({"polygon": None, "default_radius_meters": 50, "latitude": lat, "longitude": lon})

        try:
            profile: Profile | None = request.user.profile
        except Profile.DoesNotExist:
            profile = None
        if profile is None:
            return JsonResponse({"error": "User has no profile"}, status=403)

        campus, _ = Campus.objects.get_or_create(location=pin.location, profile=profile)
        if campus.polygon is None:
            campus.polygon = _default_campus_polygon(lat, lon, name=pin.effective_name)
            campus.save(update_fields=["polygon", "updated"])

        return JsonResponse(
            {
                "polygon": json.loads(campus.polygon.geojson) if campus.polygon else None,
                "default_radius_meters": campus.default_radius_meters,
                "latitude": lat,
                "longitude": lon,
            },
        )

    def save_campus(self, request: Request, pin_slug):
        """Create or update the current user's pin-detail boundary."""
        from urbanlens.dashboard.models.location.model import Location

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
            profile: Profile | None = request.user.profile
        except Profile.DoesNotExist:
            return JsonResponse({"error": "User has no profile"}, status=403)
        if profile is None:
            return JsonResponse({"error": "User has no profile"}, status=403)

        polygon_geojson = data.get("polygon")
        campus, _ = Campus.objects.get_or_create(location=pin.location, profile=profile)
        if polygon_geojson:
            geom = GEOSGeometry(json.dumps(polygon_geojson), srid=4326)
            if isinstance(geom, Polygon):
                geom = MultiPolygon(geom, srid=geom.srid)
            campus.polygon = geom
        else:
            campus.polygon = _default_campus_polygon(
                float(pin.effective_latitude),
                float(pin.effective_longitude),
                name=pin.effective_name,
            )
        campus.save(update_fields=["polygon", "updated"])

        return JsonResponse({"status": "ok", "polygon": json.loads(campus.polygon.geojson) if campus.polygon else None})

    def list_campuses(self, request: HttpRequest):
        """Return all campus boundaries visible to the current user for the main map overlay.

        Returns personal campuses for the user plus admin defaults for locations
        where the user has no personal campus.
        """
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        try:
            profile: Profile | None = request.user.profile
        except Profile.DoesNotExist:
            profile = None

        if profile:
            personal_location_ids = set(
                Campus.objects.filter(profile=profile).values_list("location_id", flat=True),
            )
            campuses = list(Campus.objects.filter(profile=profile).select_related("location")) + list(
                Campus.objects.filter(profile__isnull=True)
                .exclude(location_id__in=personal_location_ids)
                .select_related("location"),
            )
        else:
            campuses = list(Campus.objects.filter(profile__isnull=True).select_related("location"))

        result = [
            {
                "id": c.id,
                "location_id": c.location_id,
                "latitude": float(c.location.latitude),
                "longitude": float(c.location.longitude),
                "polygon": json.loads(c.polygon.geojson) if c.polygon else None,
                "default_radius_meters": c.default_radius_meters,
            }
            for c in campuses
        ]
        return JsonResponse({"campuses": result})
