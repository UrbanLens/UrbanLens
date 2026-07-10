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
from urbanlens.dashboard.services.visits import add_visited_status, resolve_location_for_point, sync_last_visited, visit_logging_allowed

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


def _resuggest_nearby_unfiled_photos(profile: Profile, pin: Pin, *, exclude_image_id: int) -> None:
    """Retroactively file or suggest visits for other unfiled photos now that a pin exists here.

    A dropped batch of photos taken at the same unpinned spot all land in the
    "needs_pin" state (see ``classify_photo``) since none of them match an
    existing pin. Once the user creates a pin from one of them, this mirrors
    what would have happened had the rest been *uploaded* after that pin
    already existed - with one adjustment for the common same-day-batch case:

    - Different day than any visit already logged at this pin: raise a normal
      VisitSuggestion via ``maybe_suggest_photo_visit`` for the uploader to
      confirm, exactly as a fresh upload would.
    - Same day as a visit already logged at this pin (the overwhelmingly
      common case - a photo drop is usually one outing): ``create_visit_suggestion``
      would silently no-op here (a same-day visit already exists and no new
      participants would be added), which would leave the photo stuck offering
      "create a pin" forever. Since it's unambiguously the same visit, file it
      directly via ``log_visit_on_pin`` instead of asking for a confirmation
      that would never actually appear.

    Args:
        profile: The photos' owner (also the new pin's owner).
        pin: The pin that was just created or reused.
        exclude_image_id: The photo already handled by the caller - skipped
            here since it's already filed.
    """
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
    from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit

    candidates = list(
        Image.objects.needs_attention(profile).exclude(pk=exclude_image_id).filter(latitude__isnull=False, longitude__isnull=False, taken_at__isnull=False),
    )
    if not candidates:
        return
    already_suggested = set(
        VisitSuggestion.objects.filter(origin_image__in=candidates, status=VisitSuggestionStatus.PENDING).values_list("origin_image_id", flat=True),
    )
    for candidate in candidates:
        if candidate.pk in already_suggested:
            continue
        matched_pin = find_matching_pin(profile, candidate.latitude, candidate.longitude)
        if matched_pin is None or matched_pin.pk != pin.pk:
            continue
        if pin.visit_history.filter(visited_at__date=candidate.taken_at.date()).exists():
            log_visit_on_pin(profile, candidate, pin)
        else:
            maybe_suggest_photo_visit(candidate)


def create_pin_and_log_visit(
    profile: Profile,
    image: Image,
    *,
    latitude: Decimal | float | None = None,
    longitude: Decimal | float | None = None,
    name: str | None = None,
) -> tuple[Pin, PinVisit | None]:
    """Create a pin for a geotagged photo and log a visit there in one step.

    Used for a photo that has GPS but matches none of the user's existing pins.
    The shared Location is resolved first so an existing pin at that exact
    place can be reused instead of colliding with ``db_pin_unique_location_per_profile``
    - this happens when another photo from the same batch already created a
    pin here (see ``_resuggest_nearby_unfiled_photos``, which is meant to catch
    this first, but a race or an out-of-order confirm-dialog submission can
    still reach this path). A minimal pin is created when none exists yet
    (copying nothing private), and a photo-sourced PinVisit is logged; the
    photo is attached to both the pin and that visit.

    The caller may override where the pin is placed and give it a name - the
    Memories confirmation dialog lets the user drag the marker and name the pin
    before committing, rather than silently dropping it at the raw photo GPS. The
    photo keeps its own coordinates (where it was taken); only the pin/Location is
    placed at the confirmed point. A caller-provided name is only applied when
    the pin doesn't already have one, so reusing an existing named pin never
    overwrites it.

    Args:
        profile: The owner the new pin and visit belong to.
        image: The geotagged photo. Its coordinates are used unless ``latitude``
            and ``longitude`` are supplied.
        latitude: Optional latitude to place the pin at (defaults to the photo's).
        longitude: Optional longitude to place the pin at (defaults to the photo's).
        name: Optional user-provided pin name; left unset to fall back to the
            Location's canonical name via ``Pin.effective_name``.

    Returns:
        The Pin (new or reused), and the new PinVisit - or None if profile has
        turned off visit-history tracking (the pin/photo association still
        happens; only the visit row is skipped).

    Raises:
        ValueError: If neither an override nor the image supplies coordinates.
    """
    lat = latitude if latitude is not None else image.effective_latitude
    lng = longitude if longitude is not None else image.effective_longitude
    if lat is None or lng is None:
        raise ValueError("create_pin_and_log_visit requires coordinates (from the image or overrides)")

    location = resolve_location_for_point(lat, lng)
    pin = Pin.objects.filter(profile=profile, location=location, parent_pin__isnull=True).select_related("location").first()
    if pin is None:
        pin = Pin.objects.create(profile=profile, location=location)
    if name and name.strip() and pin.name is None:
        pin.name = name.strip()
        pin.name_is_user_provided = True
        pin.save(update_fields=["name", "name_is_user_provided", "updated"])

    visit = PinVisit.objects.create(pin=pin, visited_at=_visit_time(image), source=VisitSource.PHOTO) if visit_logging_allowed(profile) else None
    image.pin = pin
    image.visit = visit
    # The pin's shared Location exists immediately (resolved above), so the
    # photo inherits it right away. No background wiki/boundary work happens
    # here - wikis are user-created from the pin page.
    if image.location_id is None:
        image.location = pin.location
        image.save(update_fields=["pin", "visit", "location", "updated"])
    else:
        image.save(update_fields=["pin", "visit", "updated"])
    if visit is not None:
        sync_last_visited(pin)
        add_visited_status(pin)
    _resuggest_nearby_unfiled_photos(profile, pin, exclude_image_id=image.pk)
    return pin, visit


def log_visit_on_pin(profile: Profile, image: Image, pin: Pin) -> PinVisit | None:
    """Log a photo-sourced visit on an existing pin and attach the photo to it.

    Used both for a geotagged photo the user manually assigns and for a photo with
    no GPS the user searches a pin for. When the photo has no coordinates, they are
    backfilled from the pin so it appears on the map.

    Args:
        profile: The owner the visit belongs to (also the pin owner).
        image: The photo to file.
        pin: The pin to log the visit against.

    Returns:
        The newly created PinVisit, or None if profile has turned off
        visit-history tracking (the photo is still attached to the pin).
    """
    visit = PinVisit.objects.create(pin=pin, visited_at=_visit_time(image), source=VisitSource.PHOTO) if visit_logging_allowed(profile) else None
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
    if visit is not None:
        sync_last_visited(pin)
        add_visited_status(pin)
    return visit
