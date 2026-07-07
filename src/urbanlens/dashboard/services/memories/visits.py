"""PinVisit suggestion from uploaded photos, and geolocation-based visits, for the Memories feature."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion

logger = logging.getLogger(__name__)

PHOTO_VISIT_MATCH_RADIUS_M = 100


def maybe_suggest_photo_visit(image: Image) -> VisitSuggestion | None:
    """Suggest a PinVisit when an uploaded photo has GPS + capture time near its pin.

    Rather than silently logging a visit, this raises a self-directed
    ``VisitSuggestion`` the uploader confirms or dismisses from the pin's visit
    history panel. Only runs for photos attached to one of the uploader's own
    pins (bare location-wiki uploads have no pin). The photo's own GPS is checked
    against its pin's location as a sanity check against batch-uploaded photos
    from an unrelated trip.

    No suggestion is raised when the photo doesn't qualify (missing pin,
    timestamp, or coordinates, or too far from the pin), when the uploader
    already has a visit logged for that place on the photo's capture date (the
    "relevant day" - handled by ``create_visit_suggestion``), or when an
    equivalent pending suggestion already exists (so uploading many photos from
    the same day yields at most one suggestion).

    Args:
        image: The uploaded Image, with ``pin``, ``taken_at``, ``latitude``, and
            ``longitude`` already populated.

    Returns:
        The created VisitSuggestion, or None if the photo doesn't qualify or a
        visit/suggestion already covers that day.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
    from urbanlens.dashboard.services.visits import create_visit_suggestion

    if not image.pin_id or image.taken_at is None or image.latitude is None or image.longitude is None or not image.pin:
        return None

    pin: Pin = image.pin

    photo_point = Point(float(image.longitude), float(image.latitude), srid=4326)
    within_range = Pin.objects.filter(
        pk=pin.pk,
        point__distance_lte=(photo_point, D(m=PHOTO_VISIT_MATCH_RADIUS_M)),
    ).exists()
    if not within_range:
        logger.debug("Photo %s GPS too far from pin %s, skipping visit suggestion.", image.pk, pin.pk)
        return None

    lat = pin.effective_latitude
    lng = pin.effective_longitude
    if lat is None or lng is None:
        return None

    # Collapse batch uploads from one day into a single pending suggestion.
    already_pending = (
        VisitSuggestion.objects.for_profile(pin.profile)
        .pending()
        .for_place(location=pin.location, latitude=lat, longitude=lng)
        .filter(visited_at__date=image.taken_at.date())
        .exists()
    )
    if already_pending:
        return None

    return create_visit_suggestion(
        suggested_to=pin.profile,
        suggested_by=None,
        visited_at=image.taken_at,
        location=pin.location,
        latitude=lat,
        longitude=lng,
        candidate_profiles=[],
        origin_image=image,
        origin_pin=pin,
    )
