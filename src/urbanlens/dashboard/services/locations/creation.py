"""Create and link shared Location rows for pins."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from django.contrib.gis.geos import Point, Polygon
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService, normalize_coordinate
from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain

logger = logging.getLogger(__name__)

_DEFAULT_BBOX_DEGREES = 0.00045


def _default_bbox(latitude: float, longitude: float) -> Polygon:
    delta = _DEFAULT_BBOX_DEGREES
    return Polygon.from_bbox((longitude - delta, latitude - delta, longitude + delta, latitude + delta))


@dataclass(slots=True)
class LocationCreationService:
    """Create or find a shared Location for a pin and link the pin to it."""

    name_resolver: PlaceNameResolverChain = field(default_factory=PlaceNameResolverChain)

    def create_for_pin(self, pin_id: int) -> Location | None:
        pin = Pin.objects.select_related("location").filter(pk=pin_id).first()
        if pin is None or pin.location_id or pin.is_private or pin.parent_pin_id or pin.parent_location_id:
            return pin.location if pin and pin.location_id else None

        latitude = pin.effective_latitude
        longitude = pin.effective_longitude
        if latitude is None or longitude is None:
            logger.warning("Cannot create location for pin %s without coordinates.", pin_id)
            return None

        latitude = float(latitude)
        longitude = float(longitude)
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
                        name=name,
                        official_name=place_name,
                        latitude=latitude,
                        longitude=longitude,
                        point=point,
                        bounding_box=_default_bbox(latitude, longitude),
                        google_place=google_place,
                    )
            except IntegrityError:
                location = Location.objects.get_for_point(latitude, longitude)
                if location is None:
                    raise

        if location.google_place_id is None:
            service.ensure_linked(location)
        if pin.google_place_id is None:
            service.ensure_linked(pin)

        pin_updates: dict[str, object] = {"location": location}
        if (
            normalize_coordinate(pin.latitude) == normalize_coordinate(location.latitude)
            and normalize_coordinate(pin.longitude) == normalize_coordinate(location.longitude)
            and pin.google_place_id != location.google_place_id
        ):
            pin_updates["google_place"] = location.google_place
        Pin.objects.filter(pk=pin.pk, location__isnull=True).update(**pin_updates)
        return location
