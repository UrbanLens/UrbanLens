"""Campus boundary controller - get, save, and list campus regions."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSGeometry
from django.http import HttpRequest, JsonResponse
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


class CampusController(LoginRequiredMixin, GenericViewSet):
    """API endpoints for Campus boundary data.

    Endpoints are JSON-only (no template rendering).  The campus editor UI
    is rendered by the pin detail template; these views serve its data calls.
    """

    def get_campus(self, request: HttpRequest, pin_id: int):
        """Return the effective campus for a pin's location.

        Resolution order: user's personal campus → admin default → null.
        The client should render a circle with default_radius_meters when polygon is null.
        """
        try:
            pin = Pin.objects.select_related("location").get(id=pin_id)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        lat = float(pin.location.latitude) if pin.location_id else pin.effective_latitude
        lon = float(pin.location.longitude) if pin.location_id else pin.effective_longitude

        if not pin.location_id:
            return JsonResponse({"polygon": None, "default_radius_meters": 50, "latitude": lat, "longitude": lon})

        try:
            profile = request.user.profile
        except Exception:
            profile = None

        campus = Campus.objects.effective_for(pin.location, profile)
        return JsonResponse(
            {
                "polygon": json.loads(campus.polygon.geojson) if campus and campus.polygon else None,
                "default_radius_meters": campus.default_radius_meters if campus else 50,
                "latitude": lat,
                "longitude": lon,
            }
        )

    def save_campus(self, request: HttpRequest, pin_id: int):
        """Create or update the current user's campus boundary for a pin's location."""
        try:
            pin = Pin.objects.select_related("location").get(id=pin_id)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found"}, status=404)

        if not pin.location_id:
            return JsonResponse({"error": "Pin has no linked location"}, status=400)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            profile = request.user.profile
        except Exception:
            return JsonResponse({"error": "User has no profile"}, status=403)

        polygon_geojson = data.get("polygon")
        campus, _ = Campus.objects.get_or_create(location=pin.location, profile=profile)
        campus.polygon = GEOSGeometry(json.dumps(polygon_geojson)) if polygon_geojson else None
        campus.save()

        return JsonResponse({"status": "ok"})

    def list_campuses(self, request: HttpRequest):
        """Return all campus boundaries visible to the current user for the main map overlay.

        Returns personal campuses for the user plus admin defaults for locations
        where the user has no personal campus.
        """
        try:
            profile = request.user.profile
        except Exception:
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
