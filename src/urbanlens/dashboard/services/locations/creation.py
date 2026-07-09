"""Create and link shared Location rows for pins."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import MultiPolygon
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain, default_bbox
from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def _default_bbox(latitude: float, longitude: float):
    """Backward-compatible wrapper for the deterministic boundary fallback."""
    return default_bbox(latitude, longitude)


@dataclass(slots=True)
class LocationCreationService:
    """Ensure a public pin's Location has a community wiki + boundary, and link the pin to it.

    A Pin now always references a shared Location (its coordinates live there), so
    this service no longer creates the Location. It materialises the community
    Wiki for that place, generates the default boundary Campus, and links the pin
    to the wiki - the work that used to accompany Location creation.
    """

    name_resolver: PlaceNameResolverChain = field(default_factory=PlaceNameResolverChain)
    boundary_resolver: BoundaryProviderChain = field(default_factory=BoundaryProviderChain)

    def create_for_pin(self, pin_id: int) -> Location | None:
        pin = Pin.objects.select_related("location").filter(pk=pin_id).first()
        if pin is None or pin.location_id is None or pin.is_private or pin.parent_pin_id:
            return pin.location if pin and pin.location_id else None

        location = pin.location
        latitude = float(location.latitude)
        longitude = float(location.longitude)
        service = GooglePlaceService(name_resolver=self.name_resolver)

        if location.google_place_id is None:
            service.ensure_linked(location)

        place_name = location.official_name or self.name_resolver.resolve(latitude, longitude) or "Unnamed Location"

        with transaction.atomic():
            wiki, _created = Wiki.objects.get_or_create_for_location(location, defaults={"name": place_name})
            default_campus, _ = Campus.objects.get_or_create(
                wiki=wiki,
                pin=None,
                profile=None,
                defaults={"location": location},
            )
            if default_campus.generated_polygon is None:
                try:
                    boundary = self.boundary_resolver.get_boundary(latitude, longitude, name=wiki.name or place_name)
                    default_campus.generated_polygon = MultiPolygon(boundary, srid=boundary.srid)
                    default_campus.save(update_fields=["generated_polygon", "updated"])
                except (ValueError, TypeError, IntegrityError):
                    logger.exception("Failed to generate boundary for wiki %s", wiki.pk)

        # Link the pin to its community wiki (only if it isn't linked yet).
        if pin.wiki_id is None:
            Pin.objects.filter(pk=pin.pk, wiki__isnull=True).update(wiki=wiki)
        return location
