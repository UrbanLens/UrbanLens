"""PinVisit auto-creation for the Memories feature (PHOTO and GEOLOCATION sources)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits import sync_last_visited

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

PHOTO_VISIT_MATCH_RADIUS_M = 100


def maybe_create_photo_visit(image: Image) -> PinVisit | None:
    """Create a PinVisit(source=PHOTO) when an uploaded photo has GPS + capture time near its pin.

    Only runs for photos attached to one of the uploader's own pins (bare
    location-wiki uploads have no pin to attach a visit to). The photo's own
    GPS is checked against its pin's location as a sanity check against
    batch-uploaded photos from an unrelated trip, rather than a search across
    all of the profile's pins.

    Args:
        image: The uploaded Image, with `pin`, `taken_at`, `latitude`, and
            `longitude` already populated.

    Returns:
        The created (or already-existing) PinVisit, or None if the photo
        doesn't qualify (missing pin/timestamp/coordinates, or too far from
        the pin it's attached to).
    """
    from urbanlens.dashboard.models.pin.model import Pin

    if not image.pin_id or image.taken_at is None or image.latitude is None or image.longitude is None or not image.pin:
        return None

    pin : Pin = image.pin
    
    photo_point = Point(float(image.longitude), float(image.latitude), srid=4326)
    within_range = Pin.objects.filter(
        pk=pin.pk,
        point__distance_lte=(photo_point, D(m=PHOTO_VISIT_MATCH_RADIUS_M)),
    ).exists()
    if not within_range:
        logger.debug("Photo %s GPS too far from pin %s, skipping auto-visit.", image.pk, pin.pk)
        return None

    visit, created = PinVisit.objects.get_or_create(
        pin=pin,
        visited_at=image.taken_at,
        source=VisitSource.PHOTO,
    )
    if created:
        sync_last_visited(pin)
    return visit
