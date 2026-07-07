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
    """Raise a self-directed VisitSuggestion when a geotagged, timestamped photo implies a visit.

    Dispatches on how the photo was uploaded:

    - Attached to one of the uploader's own pins (pin/location gallery upload):
      the photo's GPS is checked against that specific pin.
    - Unfiled Memories-page upload (no pin, no location): the photo's GPS is
      matched against all of the uploader's top-level pins to find one it was
      likely taken at.

    Either way, rather than silently logging a visit, a ``VisitSuggestion`` the
    uploader confirms or dismisses is raised. No suggestion is created when the
    photo lacks a timestamp or coordinates, matches no pin, when the uploader
    already has a visit logged for that place on the capture date, or when an
    equivalent pending suggestion already exists (so a same-day batch upload
    yields at most one suggestion).

    Args:
        image: The uploaded Image, with ``taken_at``, ``latitude``, and
            ``longitude`` populated (and ``pin``/``profile`` as applicable).

    Returns:
        The created VisitSuggestion, or None if the photo doesn't qualify.
    """
    if image.taken_at is None or image.latitude is None or image.longitude is None:
        return None
    if image.pin_id and image.pin:
        return _suggest_for_pinned_photo(image)
    if image.profile_id and not image.location_id:
        return _suggest_for_unfiled_photo(image)
    return None


def _suggest_for_pinned_photo(image: Image) -> VisitSuggestion | None:
    """Raise a visit suggestion for a photo uploaded directly to one of the user's pins."""
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
    from urbanlens.dashboard.services.visits import create_visit_suggestion

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


def _suggest_for_unfiled_photo(image: Image) -> VisitSuggestion | None:
    """Raise a visit suggestion for an unfiled Memories-page photo near one of the user's pins.

    Matches the photo's GPS against all of the uploader's top-level pins (via
    ``find_matching_pin``) and, on a hit, raises a self-directed suggestion for
    that pin's place. The suggestion carries ``origin_image`` so accepting it
    attaches the photo to the resulting visit.
    """
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
    from urbanlens.dashboard.services.memories.photos import find_matching_pin
    from urbanlens.dashboard.services.visits import create_visit_suggestion

    profile = image.profile
    pin = find_matching_pin(profile, image.latitude, image.longitude)
    if pin is None:
        return None

    lat = pin.effective_latitude
    lng = pin.effective_longitude
    if lat is None or lng is None:
        return None

    # Collapse a same-day batch upload into a single pending suggestion per place.
    already_pending = (
        VisitSuggestion.objects.for_profile(profile)
        .pending()
        .for_place(location=pin.location, latitude=lat, longitude=lng)
        .filter(visited_at__date=image.taken_at.date())
        .exists()
    )
    if already_pending:
        return None

    return create_visit_suggestion(
        suggested_to=profile,
        suggested_by=None,
        visited_at=image.taken_at,
        location=pin.location,
        latitude=lat,
        longitude=lng,
        candidate_profiles=[],
        origin_image=image,
        origin_pin=pin,
    )
