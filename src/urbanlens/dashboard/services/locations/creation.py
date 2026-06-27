"""Create and link shared Location rows for pins."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from django.contrib.gis.geos import Point, Polygon
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.locations.naming import PlaceNameResolverChain

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
        if location is None:
            place_name = self.name_resolver.resolve(latitude, longitude)
            name = place_name or pin.nickname or "Unnamed Location"
            try:
                with transaction.atomic():
                    location = Location.objects.create(
                        name=name,
                        latitude=latitude,
                        longitude=longitude,
                        point=point,
                        bounding_box=_default_bbox(latitude, longitude),
                        cached_place_name=place_name,
                    )
            except IntegrityError:
                location = Location.objects.get_for_point(latitude, longitude)
                if location is None:
                    raise

        Pin.objects.filter(pk=pin.pk, location__isnull=True).update(location=location)
        return location
