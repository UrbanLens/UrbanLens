"""Create and link shared Location rows for pins."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from django.contrib.gis.geos import MultiPolygon, Point
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService, normalize_coordinate
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain, default_bbox
from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain

logger = logging.getLogger(__name__)


def _default_bbox(latitude: float, longitude: float):
    """Backward-compatible wrapper for the deterministic boundary fallback."""
    return default_bbox(latitude, longitude)


@dataclass(slots=True)
class LocationCreationService:
    """Create or find a shared Location for a pin and link the pin to it."""

    name_resolver: PlaceNameResolverChain = field(default_factory=PlaceNameResolverChain)
    boundary_resolver: BoundaryProviderChain = field(default_factory=BoundaryProviderChain)

    def create_for_pin(self, pin_id: int) -> Location | None:
        pin = Pin.objects.select_related("location").filter(pk=pin_id).first()
        if pin is None or pin.location_id or pin.is_private or pin.parent_pin_id or pin.parent_wiki_id:
            return pin.location if pin and pin.location_id else None

        latitude = float(pin.effective_latitude)
        longitude = float(pin.effective_longitude)
        point = Point(longitude, latitude, srid=4326)
        location = Location.objects.get_for_point(latitude, longitude)
        service = GooglePlaceService(name_resolver=self.name_resolver)
        if location is None:
            place_name = self.name_resolver.resolve(latitude, longitude)
            name = place_name or "Unnamed Location"
            google_place = service.get_or_create_for_coordinates(
                latitude,
                longitude,
                place_name=place_name,
            )
            try:
                with transaction.atomic():
                    location = Location.objects.create(
                        official_name=place_name,
                        latitude=latitude,
                        longitude=longitude,
                        point=point,
                        google_place=google_place,
                    )
                    wiki, _created = Wiki.objects.get_or_create_for_location(location, defaults={"name": name})
                    boundary = self.boundary_resolver.get_boundary(latitude, longitude, name=name)
                    campus_polygon = MultiPolygon(boundary, srid=boundary.srid)
                    Campus.objects.create(
                        wiki=wiki,
                        location=location,
                        generated_polygon=campus_polygon,
                    )
            except IntegrityError:
                location = Location.objects.get_for_point(latitude, longitude)
                if location is None:
                    raise

        if location.google_place_id is None:
            service.ensure_linked(location)
        if pin.google_place_id is None:
            service.ensure_linked(pin)

        # This bypasses Pin.save() via .update(), so point (unchanged here since pin's
        # own effective coordinates - the point it was created from - don't change by
        # gaining a location) must still be carried explicitly to stay in sync.
        pin_updates: dict[str, object] = {"location": location, "point": point}
        try:
            pin_updates["wiki"] = location.wiki
        except ObjectDoesNotExist:
            wiki, _created = Wiki.objects.get_or_create_for_location(location)
            pin_updates["wiki"] = wiki
        if normalize_coordinate(pin.latitude) == normalize_coordinate(location.latitude) and normalize_coordinate(pin.longitude) == normalize_coordinate(location.longitude) and pin.google_place_id != location.google_place_id:
            pin_updates["google_place"] = location.google_place
        Pin.objects.filter(pk=pin.pk, location__isnull=True).update(**pin_updates)
        return location
