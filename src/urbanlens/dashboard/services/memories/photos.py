"""Organize-photos helpers for the Memories Photos page: pin matching, classification, and visit logging.

These build on the lower-level PinVisit/VisitSuggestion helpers in
``services.visits`` and are the operations the Photos page controllers call when a
user confirms, pins, or manually files an uploaded photo. Ingestion (raising a
``VisitSuggestion`` from a freshly uploaded, unfiled photo) lives in
``services.memories.visits.maybe_suggest_photo_visit``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits import add_visited_status, create_minimal_pin, sync_last_visited

if TYPE_CHECKING:
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

# Photos whose GPS falls within this many metres of one of the user's pins are
# treated as taken at that pin. Mirrors ``services.memories.visits`` so the
# suggestion path and the manual-organize path agree on what "near a pin" means.
PHOTO_PIN_MATCH_RADIUS_M = 100

PhotoState = Literal["filed", "suggested", "needs_pin", "needs_location"]


def find_matching_pin(profile: Profile, latitude: Decimal | float, longitude: Decimal | float) -> Pin | None:
    """Return the profile's nearest top-level pin within the match radius, if any.

    Args:
        profile: The pin owner to search within.
        latitude: WGS-84 latitude of the photo.
        longitude: WGS-84 longitude of the photo.

    Returns:
        The closest matching Pin, or None when the profile has no pin within
        ``PHOTO_PIN_MATCH_RADIUS_M`` metres of the point.
    """
    point = Point(float(longitude), float(latitude), srid=4326)
    return (
        Pin.objects.filter(profile=profile)
        .root_pins()
        .filter(location__point__distance_lte=(point, D(m=PHOTO_PIN_MATCH_RADIUS_M)))
        .annotate(_photo_distance=Distance("location__point", point))
        .select_related("location")
        .order_by("_photo_distance")
        .first()
    )


def classify_photo(image: Image) -> PhotoState:
    """Return the organize state of an uploaded photo.

    Args:
        image: The Image to classify (``visit``, coordinates, and
            ``organize_dismissed`` are read).

    Returns:
        - ``"filed"``: already tied to a visit, or dismissed - no action needed.
        - ``"suggested"``: has a pending photo-origin VisitSuggestion to confirm.
        - ``"needs_pin"``: geotagged but no matching pin - offer create-pin.
        - ``"needs_location"``: no coordinates - offer manual pin search.
    """
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus

    if image.visit_id or image.organize_dismissed:
        return "filed"
    if VisitSuggestion.objects.filter(origin_image=image, status=VisitSuggestionStatus.PENDING).exists():
        return "suggested"
    if image.effective_latitude is not None and image.effective_longitude is not None:
        return "needs_pin"
    return "needs_location"


def _visit_time(image: Image):
    """Return the timestamp to log a visit at for a photo (capture time, else upload time)."""
    return image.taken_at or image.created


def create_pin_and_log_visit(
    profile: Profile,
    image: Image,
    *,
    latitude: Decimal | float | None = None,
    longitude: Decimal | float | None = None,
    name: str | None = None,
) -> tuple[Pin, PinVisit]:
    """Create a pin for a geotagged photo and log a visit there in one step.

    Used for a photo that has GPS but matches none of the user's existing pins.
    A minimal pin is created (copying nothing private) and a background task
    resolves its shared Location/address; a photo-sourced PinVisit is logged and
    the photo is attached to both the new pin and that visit.

    The caller may override where the pin is placed and give it a name - the
    Memories confirmation dialog lets the user drag the marker and name the pin
    before committing, rather than silently dropping it at the raw photo GPS. The
    photo keeps its own coordinates (where it was taken); only the pin/Location is
    placed at the confirmed point.

    Args:
        profile: The owner the new pin and visit belong to.
        image: The geotagged photo. Its coordinates are used unless ``latitude``
            and ``longitude`` are supplied.
        latitude: Optional latitude to place the pin at (defaults to the photo's).
        longitude: Optional longitude to place the pin at (defaults to the photo's).
        name: Optional user-provided pin name; left unset to fall back to the
            Location's canonical name via ``Pin.effective_name``.

    Returns:
        The newly created Pin and PinVisit.

    Raises:
        ValueError: If neither an override nor the image supplies coordinates.
    """
    lat = latitude if latitude is not None else image.effective_latitude
    lng = longitude if longitude is not None else image.effective_longitude
    if lat is None or lng is None:
        raise ValueError("create_pin_and_log_visit requires coordinates (from the image or overrides)")

    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import create_location_for_pin

    pin = create_minimal_pin(profile, location=None, latitude=lat, longitude=lng)
    if name and name.strip():
        pin.name = name.strip()
        pin.name_is_user_provided = True
        pin.save(update_fields=["name", "name_is_user_provided", "updated"])
    safely_enqueue_task(create_location_for_pin, pin.pk)

    visit = PinVisit.objects.create(pin=pin, visited_at=_visit_time(image), source=VisitSource.PHOTO)
    image.pin = pin
    image.visit = visit
    # The pin's shared Location is resolved by create_location_for_pin in the
    # background, which also backfills image.location for the pin's photos.
    image.save(update_fields=["pin", "visit", "updated"])
    sync_last_visited(pin)
    add_visited_status(pin)
    return pin, visit


def log_visit_on_pin(profile: Profile, image: Image, pin: Pin) -> PinVisit:
    """Log a photo-sourced visit on an existing pin and attach the photo to it.

    Used both for a geotagged photo the user manually assigns and for a photo with
    no GPS the user searches a pin for. When the photo has no coordinates, they are
    backfilled from the pin so it appears on the map.

    Args:
        profile: The owner the visit belongs to (also the pin owner).
        image: The photo to file.
        pin: The pin to log the visit against.

    Returns:
        The newly created PinVisit.
    """
    visit = PinVisit.objects.create(pin=pin, visited_at=_visit_time(image), source=VisitSource.PHOTO)
    update_fields = ["pin", "visit", "updated"]
    image.pin = pin
    image.visit = visit
    if image.location_id is None and pin.location_id is not None:
        image.location = pin.location
        update_fields.append("location")
    if image.latitude is None or image.longitude is None:
        image.latitude = pin.location.latitude
        image.longitude = pin.location.longitude
        update_fields.extend(["latitude", "longitude"])
    image.save(update_fields=update_fields)
    sync_last_visited(pin)
    add_visited_status(pin)
    return visit
