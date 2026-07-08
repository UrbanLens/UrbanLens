"""Business logic for PinVisit / VisitSuggestion: privacy-safe messaging, pin dedup, and accept/reject."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.connections import are_connections
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    import datetime
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.models.trips.model import TripActivity

logger = logging.getLogger(__name__)


def build_visit_suggestion_message(*, location: Location | None = None, **kwargs: str | None) -> str:
    """Build a privacy-safe place description for a visit-suggestion notification.

    Prefers the shared Location. When there is no Location - e.g. the origin pin is
    private and was never linked to one - falls back to the origin pin's own
    official_name/city/state. Pin.official_name is populated only from external
    APIs, exactly like Location.official_name, and is never the user's private
    custom label; Pin.name and PinVisit.notes are never read here or anywhere else
    in this module, so this function is structurally incapable of leaking them.

    Args:
        location: Shared Location identifying the place, if one exists. Wins over
            official_name/city/state below whenever present.
        **kwargs: Additional keyword arguments for fallback values.

    Returns:
        A short phrase like "at Old Mill" or "in Springfield, IL", or a generic
        fallback when no usable name or city/state is available from either source.
    """
    official_name = location.official_name if location else kwargs.get("official_name")
    canonical_name = kwargs.get("canonical_name")
    if location is not None:
        # A Location's wiki is a reverse OneToOne accessor - it raises
        # Wiki.DoesNotExist rather than evaluating falsy when unset.
        try:
            wiki = location.wiki
        except ObjectDoesNotExist:
            wiki = None
        canonical_name = wiki.name if wiki is not None else None
    city = location.city if location else kwargs.get("city")
    state = location.state if location else kwargs.get("state")

    if is_meaningful_name(official_name):
        return f"at {official_name}"
    if is_meaningful_name(canonical_name):
        return f"at {canonical_name}"
    if city and state:
        return f"in {city}, {state}"
    if city:
        return f"in {city}"
    return "at a location"


def find_pin_at(profile: Profile, *, location_id: int | None = None, latitude: float | Decimal | None = None, longitude: float | Decimal | None = None) -> Pin | None:
    """Return a profile's own (non-detail) pin at a place, if one exists.

    Args:
        profile: Profile to check.
        location_id: Shared Location id to match on, if known.
        latitude: Latitude to match on when no location id is available/matched.
        longitude: Longitude to match on when no location id is available/matched.

    Returns:
        The matching Pin, or None.
    """
    qs = Pin.objects.filter(profile=profile, parent_pin__isnull=True, parent_wiki__isnull=True)
    if location_id:
        pin = qs.filter(location_id=location_id).first()
        if pin:
            return pin
    if latitude is not None and longitude is not None:
        # A Pin's coordinates live on its Location.
        return qs.filter(location__latitude=latitude, location__longitude=longitude).first()
    return None


def pin_exists_at(profile: Profile, *, location_id: int | None = None, latitude: float | Decimal | None = None, longitude: float | Decimal | None = None) -> bool:
    """Return whether a profile already has a (non-detail) pin at a place.

    Args:
        profile: Profile to check.
        location_id: Shared Location id to match on, if known.
        latitude: Latitude to match on when no location id is available/matched.
        longitude: Longitude to match on when no location id is available/matched.

    Returns:
        True if a matching pin already exists for this profile.
    """
    return find_pin_at(profile, location_id=location_id, latitude=latitude, longitude=longitude) is not None


def find_existing_visit_on_date(profile: Profile, *, location: Location | None, latitude: float | Decimal, longitude: float | Decimal, visited_at: datetime.datetime) -> PinVisit | None:
    """Return a profile's own visit already logged for this place on this calendar date, if any.

    Args:
        profile: Profile to check.
        location: Shared Location identifying the place, if one exists.
        latitude: Latitude of the place.
        longitude: Longitude of the place.
        visited_at: The date (and time) the new suggestion claims the visit occurred.

    Returns:
        The most recent matching PinVisit, or None if the profile has no pin here
        or no visit logged on that date.
    """
    pin = find_pin_at(profile, location_id=location.pk if location else None, latitude=latitude, longitude=longitude)
    if pin is None:
        return None
    return pin.visit_history.filter(visited_at__date=visited_at.date()).order_by("-visited_at").first()


def create_minimal_pin(profile: Profile, *, location: Location | None, latitude: float | Decimal, longitude: float | Decimal) -> Pin:
    """Create a bare pin for a profile at a place, deliberately copying nothing private.

    This leaves ``name`` unset so ``effective_name`` falls back to the Location's own name.

    Args:
        profile: Profile the new pin belongs to.
        location: Shared Location to attach, if one exists.
        latitude: Latitude for the new pin.
        longitude: Longitude for the new pin.

    Returns:
        The newly created Pin.
    """
    if location is None:
        from urbanlens.dashboard.models.location.model import Location

        location = Location.objects.get_for_point(float(latitude), float(longitude))
        if location is None:
            from urbanlens.dashboard.controllers.maps import _create_location_with_canonical_name

            location = _create_location_with_canonical_name(float(latitude), float(longitude))
    return Pin.objects.create(profile=profile, location=location)


def get_or_create_pin_at(profile: Profile, *, location: Location | None, latitude: float | Decimal, longitude: float | Decimal) -> Pin:
    """Return a profile's existing pin at a place, creating a minimal one if needed.

    Args:
        profile: Profile the pin belongs to.
        location: Shared Location identifying the place, if one exists.
        latitude: Latitude of the place.
        longitude: Longitude of the place.

    Returns:
        The existing or newly created Pin.
    """
    pin = find_pin_at(profile, location_id=location.pk if location else None, latitude=latitude, longitude=longitude)
    return pin or create_minimal_pin(profile, location=location, latitude=latitude, longitude=longitude)


def _mutual_candidates(suggested_to: Profile, suggested_by: Profile | None, candidate_profiles: list[Profile]) -> dict[int, Profile]:
    """Return, keyed by pk, the candidates who are mutual connections of suggested_to.

    Args:
        suggested_to: The profile being asked to confirm the visit.
        suggested_by: Profile who proposed the suggestion, if known.
        candidate_profiles: Other profiles from the same batch.

    Returns:
        Mapping of profile id to Profile, for every candidate (including
        suggested_by) that suggested_to is mutually connected with.
    """
    combined = list(candidate_profiles)
    if suggested_by:
        combined.append(suggested_by)
    return {p.pk: p for p in combined if are_connections(suggested_to, p)}


def create_visit_suggestion(
    *,
    suggested_to: Profile,
    suggested_by: Profile | None,
    visited_at: datetime.datetime,
    location: Location | None,
    latitude: float | Decimal,
    longitude: float | Decimal,
    candidate_profiles: list[Profile],
    origin_visit: PinVisit | None = None,
    trip_activity: TripActivity | None = None,
    safety_checkin: SafetyCheckin | None = None,
    origin_image: Image | None = None,
    origin_pin: Pin | None = None,
) -> VisitSuggestion | None:
    """Create a VisitSuggestion, and its delivery notification unless the recipient opted out.

    Exactly one of ``origin_visit``/``trip_activity``/``safety_checkin``/``origin_image``
    must be given - it determines both which flow raised this suggestion and, on
    acceptance, which VisitSource the resulting PinVisit gets (see
    ``_visit_source_for``).

    If suggested_to already has a visit logged for this place on this date, and
    every mutually-connected candidate this suggestion would add is already listed
    as a participant on that visit, nothing would change by accepting - so no
    suggestion or notification is created at all. Otherwise, if suggested_to has
    such a visit but it *would* gain new participants, the suggestion is linked to
    it via ``existing_visit`` so the recipient is offered a merge-or-separate choice
    instead of a plain accept/reject.

    Args:
        suggested_to: Profile being asked to confirm the visit.
        suggested_by: Profile who proposed this suggestion, if known.
        visited_at: When the visit is claimed to have occurred.
        location: Shared Location identifying the place, if one exists.
        latitude: Latitude of the place.
        longitude: Longitude of the place.
        candidate_profiles: Other profiles from the same batch, minus suggested_to.
        origin_visit: The suggester's own PinVisit, for the manual-dialog flow.
        trip_activity: The completed TripActivity, for the trip flow.
        safety_checkin: The concluded SafetyCheckin, for the safety check-in flow.
        origin_image: The uploaded photo, for the geotagged-photo flow (a
            self-directed suggestion where ``suggested_to`` is the uploader).
        origin_pin: The suggester's own pin, used only as a message fallback when
            there is no Location (e.g. a private, unlinked pin).

    Returns:
        The created VisitSuggestion, or None if nothing would change for suggested_to.
    """
    existing_visit = find_existing_visit_on_date(suggested_to, location=location, latitude=latitude, longitude=longitude, visited_at=visited_at)

    mutual = _mutual_candidates(suggested_to, suggested_by, candidate_profiles)
    if existing_visit:
        existing_participant_ids = set(existing_visit.participants.values_list("pk", flat=True))
        if not (set(mutual) - existing_participant_ids):
            return None

    suggestion = VisitSuggestion.objects.create(
        location=location,
        latitude=latitude,
        longitude=longitude,
        visited_at=visited_at,
        suggested_by=suggested_by,
        suggested_to=suggested_to,
        origin_visit=origin_visit,
        trip_activity=trip_activity,
        safety_checkin=safety_checkin,
        origin_image=origin_image,
        existing_visit=existing_visit,
    )
    suggestion.candidate_profiles.set(candidate_profiles)

    try:
        pref = suggested_to.notification_preferences.visit_suggested
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return suggestion

    place = build_visit_suggestion_message(location=location, official_name=origin_pin.official_name if origin_pin else None, city=origin_pin.city if origin_pin else None, state=origin_pin.state if origin_pin else None)
    when = visited_at.strftime("%b %d, %Y")
    if safety_checkin is not None:
        title = "Confirm your visit?"
        message = f"Your safety check-in wrapped up - did you make it {place} on {when}?"
    elif origin_image is not None:
        title = "Confirm your visit?"
        message = f"A photo you added looks like you visited {place} on {when}. Add it to your visit history?"
    elif existing_visit:
        who = suggested_by.username if suggested_by else "A connection"
        title = "Update your visit?"
        message = f"{who} says you were also {place} on {when}, which you already logged. Add them to that visit, or log it separately?"
    else:
        who = suggested_by.username if suggested_by else "A connection"
        title = "Visit suggestion"
        message = f"{who} suggested you also visited {place} on {when}."
    notification = NotificationLog.objects.create(
        profile=suggested_to,
        source_profile=suggested_by,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.VISIT_SUGGESTED,
        title=title,
        message=message,
    )
    suggestion.notification = notification
    suggestion.save(update_fields=["notification", "updated"])
    return suggestion


def _visit_source_for(suggestion: VisitSuggestion) -> str:
    """Return the VisitSource the resulting PinVisit should use.

    Args:
        suggestion: The suggestion being accepted.

    Returns:
        VisitSource.TRIP, VisitSource.SAFETY_CHECKIN, VisitSource.PHOTO, or
        VisitSource.USER depending on which origin is set (the model's check
        constraint guarantees exactly one of the four is set).
    """
    if suggestion.trip_activity_id:
        return VisitSource.TRIP
    if suggestion.safety_checkin_id:
        return VisitSource.SAFETY_CHECKIN
    if suggestion.origin_image_id:
        return VisitSource.PHOTO
    return VisitSource.USER


def accept_visit_suggestion(suggestion: VisitSuggestion, accepting_profile: Profile) -> PinVisit:
    """Accept a visit suggestion by logging a new, separate PinVisit.

    Ensures a pin exists at the suggested place (reusing suggested_to's existing
    one there, including ``suggestion.existing_visit``'s own pin, if any), then
    always creates a brand-new PinVisit - used both for first-time suggestions and
    for the "log separately" choice offered when a same-day visit already exists.

    Args:
        suggestion: The pending suggestion being accepted.
        accepting_profile: The profile accepting (must be suggestion.suggested_to).

    Returns:
        The newly created PinVisit for the accepting profile.
    """
    pin = get_or_create_pin_at(accepting_profile, location=suggestion.location, latitude=suggestion.latitude, longitude=suggestion.longitude)

    visit = PinVisit.objects.create(pin=pin, visited_at=suggestion.visited_at, source=_visit_source_for(suggestion))
    sync_last_visited(pin)
    add_visited_status(pin)

    # A photo-raised suggestion carries the originating photo; on confirmation,
    # attach it to the visit so it appears on the visit row and in the gallery.
    if suggestion.origin_image_id:
        from urbanlens.dashboard.models.images.model import Image

        Image.objects.filter(pk=suggestion.origin_image_id).update(visit=visit)

    mutual = _mutual_candidates(accepting_profile, suggestion.suggested_by, list(suggestion.candidate_profiles.all()))
    visit.participants.set(mutual.values())

    suggestion.status = VisitSuggestionStatus.ACCEPTED
    suggestion.save(update_fields=["status", "updated"])
    return visit


def merge_visit_suggestion(suggestion: VisitSuggestion, accepting_profile: Profile) -> PinVisit:
    """Accept a visit suggestion by adding its new participants to an existing visit.

    Args:
        suggestion: The pending suggestion being accepted (must have existing_visit set).
        accepting_profile: The profile accepting (must be suggestion.suggested_to).

    Returns:
        The existing PinVisit, with new mutually-connected participants added.

    Raises:
        ValueError: If suggestion.existing_visit is not set - callers must check
            suggestion.offers_merge before calling this.
    """
    visit = suggestion.existing_visit
    if visit is None:
        raise ValueError("merge_visit_suggestion requires suggestion.existing_visit to be set")

    mutual = _mutual_candidates(accepting_profile, suggestion.suggested_by, list(suggestion.candidate_profiles.all()))
    existing_participant_ids = set(visit.participants.values_list("pk", flat=True))
    new_participants = [p for pk, p in mutual.items() if pk not in existing_participant_ids]
    if new_participants:
        visit.participants.add(*new_participants)

    suggestion.status = VisitSuggestionStatus.ACCEPTED
    suggestion.save(update_fields=["status", "updated"])
    return visit


def reject_visit_suggestion(suggestion: VisitSuggestion) -> None:
    """Reject a pending visit suggestion.

    Args:
        suggestion: The pending suggestion being rejected.
    """
    suggestion.status = VisitSuggestionStatus.REJECTED
    suggestion.save(update_fields=["status", "updated"])


def add_visited_status(pin: Pin) -> None:
    """Add the profile's "Visited" status badge to the pin if not already present.

    Args:
        pin: Pin instance whose statuses should be updated.
    """
    from urbanlens.dashboard.models.badges.model import Badge

    visited_badge = Badge.objects.filter(profile=pin.profile, kind="status", name="Visited").first()
    if visited_badge and not pin.badges.filter(pk=visited_badge.pk).exists():
        pin.badges.add(visited_badge)


def remove_visited_status(pin: Pin) -> None:
    """Clear a pin's "Visited" marking - the profile's status badge and last_visited.

    Used when a pin was marked visited by mistake (e.g. a stray status badge or
    an import glitch) and the user doesn't want to log a dated visit for it, so
    it should stop being surfaced in the Memories "log your visits" queue.

    Args:
        pin: Pin instance to update in-place.
    """
    from urbanlens.dashboard.models.badges.model import Badge

    visited_badge = Badge.objects.filter(profile=pin.profile, kind="status", name="Visited").first()
    if visited_badge:
        pin.badges.remove(visited_badge)
    pin.last_visited = None
    pin.save(update_fields=["last_visited"])


def sync_last_visited(pin: Pin) -> None:
    """Recompute pin.last_visited from the most recent PinVisit row.

    Args:
        pin: Pin instance to update in-place (saves only last_visited field).
    """
    latest = pin.visit_history.order_by("-visited_at").values_list("visited_at", flat=True).first()
    pin.last_visited = latest
    pin.save(update_fields=["last_visited"])


def record_geolocation_pin_visits(profile: Profile, *, latitude: float | Decimal, longitude: float | Decimal, visited_at: datetime.datetime | None = None) -> list[PinVisit]:
    """Record geolocation-based visits for the profile's pins containing a point.

    A visit is created for each of the user's top-level pins whose pin-specific
    campus boundary, location-default campus boundary, or 50 m coordinate
    fallback contains the supplied latitude/longitude. Existing visits on the
    same calendar day prevent another row from being created for that pin.

    Args:
        profile: Profile whose personal pins should be checked.
        latitude: Device-provided WGS-84 latitude.
        longitude: Device-provided WGS-84 longitude.
        visited_at: Timestamp to store; defaults to ``timezone.now()``.

    Returns:
        List of newly-created ``PinVisit`` rows.
    """
    from django.contrib.gis.geos import Point
    from django.contrib.gis.measure import D
    from django.utils import timezone

    from urbanlens.dashboard.models.campus.model import Campus

    timestamp = visited_at or timezone.now()
    point = Point(float(longitude), float(latitude), srid=4326)
    pins = Pin.objects.filter(profile=profile).root_pins().select_related("location").prefetch_related("campus")
    created_visits: list[PinVisit] = []

    for pin in pins:
        if pin.visit_history.filter(visited_at__date=timestamp.date()).exists():
            continue

        campus = Campus.objects.effective_for_pin(pin)
        if campus is not None:
            contains_point = campus.effective_polygon.contains(point)
        else:
            contains_point = Pin.objects.filter(pk=pin.pk, location__point__distance_lte=(point, D(m=50))).exists()

        if not contains_point:
            continue

        visit = PinVisit.objects.create(pin=pin, visited_at=timestamp, source=VisitSource.GEOLOCATION)
        sync_last_visited(pin)
        add_visited_status(pin)
        created_visits.append(visit)

    return created_visits
