"""
Shared placeless places: provisional and anchored tiers (ADR35 section 2, UL-367).

Everything here runs in Celery (``tasks.evaluate_shared_places_for_pin`` and the nightly sweep), never in a request or page view, so
no response can reveal whether a tier formed, when, or whose pin took part. The only thing a request ever does is the owner accepting or
declining their own :class:`NotesOffer` (:func:`answer_notes_offer`).

A synthetic place never resolves onto a ``Location`` (``PlaceQuerySet.resolvable`` excludes it). Its members reach its wiki through
``SharedPlaceMember``; one that is ``PART_OF`` a plausible parcel is also reached through the parcel's domain, as a building is (R-52).
"""

from __future__ import annotations

from datetime import timedelta
import logging
import math
import secrets

from django.conf import settings
from django.contrib.gis.db.models.functions import GeoHash
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.contrib.gis.measure import D
from django.db import connection, transaction
from django.db.models import F
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import STRONG_EVIDENCE, SYNTHETIC_EVIDENCE, Place, PlaceEvidence, PlaceKind, PlaceRelation, PlaceStatus
from urbanlens.dashboard.models.place.shared import NotesOffer, NotesOfferStatus, SharedPlaceAttempt, SharedPlaceFlag, SharedPlaceMember
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH
from urbanlens.dashboard.services.geo.area import buffer_meters
from urbanlens.dashboard.services.locations.site_scope import meters_between
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.trust.checks import are_independent, is_real_user
from urbanlens.dashboard.services.trust.signals import CELL_PRECISION
from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting

logger = logging.getLogger(__name__)

#: Serialises place creation so two evaluations of one cluster cannot each make a place.
_CREATION_LOCK_KEY = 367_001
#: Tries at a random hub coordinate before giving up (a collision at 6 decimals is vanishingly rare).
_HUB_ATTEMPTS = 60


def _radius(evidence: str) -> float:
    return settings.UL_SHARED_PLACE_ANCHORED_RADIUS_M if evidence == PlaceEvidence.ANCHORED else settings.UL_SHARED_PLACE_PROVISIONAL_RADIUS_M


def _distance(a: Point, b: Point) -> float:
    return meters_between(a.y, a.x, b.y, b.x)


def maturity_cutoff():
    """Pins created at or before this are old enough on age alone."""
    return timezone.now() - timedelta(days=settings.UL_SHARED_PLACE_MIN_PIN_AGE_DAYS)


def pin_has_matured(pin: Pin) -> bool:
    """Whether the pin is old enough, or has a photo-verified visit; a fresh pin is cheap to scatter."""
    if pin.created <= maturity_cutoff():
        return True
    return PinVisit.objects.filter(pin=pin, tentative=False, source=VisitSource.PHOTO).exists()


def pin_is_placeless(location: Location) -> bool:
    """Whether nothing with a building outline sits under the pin; a parcel alone does not count as a place for the pin."""
    resolved = Place.objects.resolve_for_point(float(location.latitude), float(location.longitude))
    return resolved is None or resolved.kind != PlaceKind.BUILDING


def _cheap_gates(pin: Pin) -> bool:
    """The gates that cost no spatial lookup: a location, community on, and maturity."""
    return pin.location_id is not None and pin.profile.community_enabled and pin_has_matured(pin)


def _pin_qualifies(pin: Pin) -> bool:
    """All pin-level gates, cheapest first: community on, a location, mature, nothing building-like under it, then a real user behind it."""
    return _cheap_gates(pin) and pin_is_placeless(pin.location) and is_real_user(pin.profile)


def _attempts_in_window(profile: Profile) -> int:
    since = timezone.now() - timedelta(hours=settings.UL_SHARED_PLACE_ATTEMPT_WINDOW_HOURS)
    return SharedPlaceAttempt.objects.filter(profile=profile, created__gte=since).count()


def _record_attempt(pin: Pin) -> bool:
    """
    Count this pin's evaluation against a candidate, positive or negative, within the rolling window.

    Returns:
        False when the account is over its limit: the profile is flagged and the evaluation is deferred, to be retried by a later sweep
        once the window rolls, never dropped. Nothing is learned from a deferral by anyone.
    """
    since = timezone.now() - timedelta(hours=settings.UL_SHARED_PLACE_ATTEMPT_WINDOW_HOURS)
    if SharedPlaceAttempt.objects.filter(pin=pin, created__gte=since).exists():
        return True
    if _attempts_in_window(pin.profile) >= settings.UL_SHARED_PLACE_ATTEMPT_LIMIT:
        flag, _ = SharedPlaceFlag.objects.get_or_create(profile=pin.profile)
        SharedPlaceFlag.objects.filter(pk=flag.pk).update(deferred_count=F("deferred_count") + 1, last_deferred_at=timezone.now())
        logger.warning("Shared-place evaluation deferred for profile %s (over %s attempts in %sh)", pin.profile_id, settings.UL_SHARED_PLACE_ATTEMPT_LIMIT, settings.UL_SHARED_PLACE_ATTEMPT_WINDOW_HOURS)
        return False
    SharedPlaceAttempt.objects.create(profile=pin.profile, pin=pin)
    return True


def _has_headroom(profile: Profile) -> bool:
    """Whether a partner's account is under its own limit; a partner at the limit defers the pair instead of failing it."""
    return _attempts_in_window(profile) < settings.UL_SHARED_PLACE_ATTEMPT_LIMIT


def _cell_of(location: Location) -> str | None:
    return Location.objects.filter(pk=location.pk).annotate(cell=GeoHash("point", precision=CELL_PRECISION)).values_list("cell", flat=True).first()


def _nearest_synthetic_place(point: Point) -> Place | None:
    """The synthetic place whose anchor is nearest *point* and within its tier's radius."""
    reach = max(settings.UL_SHARED_PLACE_PROVISIONAL_RADIUS_M, settings.UL_SHARED_PLACE_ANCHORED_RADIUS_M)
    best: tuple[float, Place] | None = None
    for place in Place.objects.filter(evidence__in=SYNTHETIC_EVIDENCE, status=PlaceStatus.CURRENT, anchor__dwithin=(point, D(m=reach))):
        if place.anchor is None:
            continue
        gap = _distance(place.anchor, point)
        if gap <= _radius(place.evidence) and (best is None or gap < best[0]):
            best = (gap, place)
    return best[1] if best else None


def _nearest_reference(point: Point) -> Point | None:
    """The nearest stored public reference (a Google place) within the anchored radius."""
    reach = settings.UL_SHARED_PLACE_ANCHORED_RADIUS_M
    span = reach / 111_320.0 * 1.5
    # A degree of longitude shrinks with cos(latitude); clamp so the window stays finite near the poles.
    lon_span = span / max(math.cos(math.radians(point.y)), 0.05)
    nearby = GooglePlace.objects.filter(latitude__range=(point.y - span, point.y + span), longitude__range=(point.x - lon_span, point.x + lon_span))
    best: tuple[float, Point] | None = None
    for ref in nearby:
        ref_point = Point(float(ref.longitude), float(ref.latitude), srid=4326)
        gap = _distance(ref_point, point)
        if gap <= reach and (best is None or gap < best[0]):
            best = (gap, ref_point)
    return best[1] if best else None


def _pins_near(point: Point, radius: float, *, excluding: Profile) -> list[Pin]:
    """Other accounts' mature pins near *point*, oldest first; fresh pins are dropped before anything costlier runs."""
    pins = Pin.objects.filter(location__point__dwithin=(point, D(m=radius)), created__lte=maturity_cutoff()).exclude(profile=excluding).select_related("location", "profile").order_by("created", "pk")
    return [pin for pin in pins if _distance(pin.location.point, point) <= radius]


def evaluate_pin(pin_id: int, *, revalidate: bool = True) -> Place | None:
    """
    Run the trust checks and tier evaluation for one pin. Celery only.

    Joins the pin's profile to an existing synthetic place by distance to its single anchor, or forms a new anchored then provisional
    place from a qualifying, independent pair of profiles.

    Args:
        pin_id: The pin to evaluate.
        revalidate: Whether to re-check the owner's memberships first; a sweep does that once per profile instead.

    Returns:
        The place the profile joined or formed, else None. The return value never leaves the worker.
    """
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    if pin is None or pin.location_id is None:
        return None
    if revalidate:
        revalidate_memberships(pin.profile)
    if not _cheap_gates(pin):
        return None
    point = pin.location.point

    existing = _nearest_synthetic_place(point)
    reference = None if existing is not None else _nearest_reference(point)
    neighbours = [] if existing is not None else _pins_near(reference if reference is not None else point, _radius(PlaceEvidence.ANCHORED if reference is not None else PlaceEvidence.PROVISIONAL), excluding=pin.profile)
    if existing is None and not neighbours:
        return None
    if not _record_attempt(pin):
        return None
    if not pin_is_placeless(pin.location) or not is_real_user(pin.profile):
        return None

    if existing is not None:
        return _join(existing, pin)
    if reference is not None:
        formed = _form(pin, reference, PlaceEvidence.ANCHORED, neighbours)
        if formed is not None:
            return formed
    return _form(pin, None, PlaceEvidence.PROVISIONAL, _pins_near(point, _radius(PlaceEvidence.PROVISIONAL), excluding=pin.profile) if reference is not None else neighbours)


def _form(pin: Pin, reference: Point | None, evidence: str, candidates: list[Pin]) -> Place | None:
    """Pair *pin* with the first qualifying independent partner among *candidates* and create the place."""
    radius = _radius(evidence)
    for other in candidates:
        if not _has_headroom(other.profile):
            continue
        if reference is None and _distance(pin.location.point, other.location.point) > radius:
            continue
        if not _pin_qualifies(other):
            continue
        if not are_independent(pin.profile, other.profile, exclude_cell=_cell_of(pin.location)):
            continue
        earlier = min((pin, other), key=lambda candidate: (candidate.created, candidate.pk))
        anchor = reference if reference is not None else earlier.location.point
        return _create_place(anchor, evidence, [pin, other])
    return None


def _independent_of_all_members(place: Place, pin: Pin) -> bool:
    """A joiner must look independent of every existing member, not only of whoever formed the place."""
    cell = _cell_of(pin.location)
    members = Profile.objects.filter(shared_place_memberships__place=place).exclude(pk=pin.profile_id)
    return all(are_independent(pin.profile, member, exclude_cell=cell) for member in members)


def _join(place: Place, pin: Pin) -> Place | None:
    """Admit the pin's profile by distance to the anchor alone, provided it is independent of every current member."""
    if SharedPlaceMember.objects.filter(place=place, profile=pin.profile).exists():
        return place
    if not _independent_of_all_members(place, pin):
        return None
    with transaction.atomic():
        _, created = SharedPlaceMember.objects.get_or_create(place=place, profile=pin.profile)
        if created:
            _offer_notes(place, pin)
    return place


def _lock_creation() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_CREATION_LOCK_KEY])


def _circle(anchor: Point, radius: float) -> MultiPolygon:
    ring = buffer_meters(Point(anchor.x, anchor.y, srid=4326), radius)
    polygon = ring if isinstance(ring, Polygon) else Polygon.from_bbox((anchor.x - 0.0002, anchor.y - 0.0002, anchor.x + 0.0002, anchor.y + 0.0002))
    return MultiPolygon(polygon, srid=4326)


def _random_offset_point(anchor: Point, radius: float) -> tuple[float, float]:
    """A point uniform over the disc of *radius* metres around *anchor*, from the OS random source."""
    resolution_steps = 1_000_000
    angle = 2 * math.pi * secrets.randbelow(resolution_steps) / resolution_steps
    distance = radius * math.sqrt(secrets.randbelow(resolution_steps) / resolution_steps)
    latitude = anchor.y + distance * math.sin(angle) / 111_320.0
    longitude = anchor.x + distance * math.cos(angle) / (111_320.0 * max(math.cos(math.radians(anchor.y)), 0.01))
    return round(latitude, 6), round(longitude, 6)


def _hub_location(anchor: Point, radius: float) -> Location:
    """
    A brand-new Location at a random point within the place's radius, chosen once.

    Never at the anchor (the earlier pin's exact point) and never an existing row, so no page about the hub can show where a member pinned.
    """
    for _ in range(_HUB_ATTEMPTS):
        latitude, longitude = _random_offset_point(anchor, radius)
        location, created = Location.objects.get_exact_or_create(latitude, longitude)
        if created:
            return location
    raise RuntimeError("no free hub coordinate within the place")


def _create_place(anchor: Point, evidence: str, pins: list[Pin]) -> Place | None:
    with transaction.atomic():
        _lock_creation()
        existing = _nearest_synthetic_place(anchor)
        if existing is not None:
            for pin in pins:
                _join(existing, pin)
            return existing
        parent = _official_parent(anchor)
        place = Place.objects.create(kind=PlaceKind.SITE, evidence=evidence, geometry=_circle(anchor, _radius(evidence)), anchor=Point(anchor.x, anchor.y, srid=4326))
        resolution.refresh_area(place)
        if parent is not None:
            lineage.set_parent(place, parent, PlaceRelation.PART_OF)
        hub = _hub_location(anchor, _radius(evidence))
        # Location.save resolved the hub onto the official parcel; the hub belongs to the shared place alone (see resolution.pinned_to_synthetic_place).
        Location.objects.filter(pk=hub.pk).update(place=place)
        hub.place = place
        wiki = Wiki.objects.create(location=hub, place=place, name="Shared place")
        if parent is not None:
            # Like a building's, the wiki nests under the parcel's and is reached through the parcel's domain (R-52).
            reconcile_wiki_nesting(wiki)
        for pin in pins:
            SharedPlaceMember.objects.get_or_create(place=place, profile=pin.profile)
        for pin in pins:
            _offer_notes(place, pin)
    return place


def _official_parent(anchor: Point) -> Place | None:
    """The plausible official parcel enclosing the anchor with no building under it; otherwise the place is top level."""
    resolved = Place.objects.resolve_for_point(anchor.y, anchor.x)
    if resolved is None or resolved.kind == PlaceKind.BUILDING or resolved.evidence not in STRONG_EVIDENCE:
        return None
    return resolved


def _hub_url(place: Place) -> str:
    wiki = Wiki.objects.filter(place=place).select_related("location").first()
    if wiki is None:
        return ""
    try:
        return reverse("location.wiki", kwargs={"location_slug": wiki.location.slug or str(wiki.location.uuid)})
    except NoReverseMatch:
        return ""


def _offer_notes(place: Place, pin: Pin) -> None:
    """Tell a newly admitted member someone else pinned here too, and offer to bring their own exact-point notes. Identical for everyone."""
    own = Wiki.objects.filter(location=pin.location).exclude(place=place).first()
    if own is not None and (own.description or "").strip():
        NotesOffer.objects.get_or_create(profile=pin.profile, place=place, defaults={"source_location": pin.location})
    NotificationLog.objects.notify(
        profile=pin.profile,
        notification_type=NotificationType.INFO,
        title="Someone else pinned this too",
        message="Another explorer has a pin at this spot. A shared page is ready, and you can choose whether to bring your own notes into it.",
        url=_hub_url(place),
    )


def revalidate_memberships(profile: Profile) -> int:
    """Drop memberships whose profile no longer holds a pin within the place's reach of its anchor. Returns how many were dropped."""
    dropped = 0
    for member in SharedPlaceMember.objects.filter(profile=profile).select_related("place"):
        anchor = member.place.anchor
        if anchor is None:
            continue
        reach = _radius(member.place.evidence)
        near = Pin.objects.filter(profile=profile, location__point__dwithin=(anchor, D(m=reach)))
        if not any(_distance(p.location.point, anchor) <= reach for p in near.select_related("location")):
            member.delete()
            dropped += 1
    return dropped


def merged_description(shared: str | None, notes: str) -> str:
    """The shared description with *notes* added once, truncated to the column limit."""
    shared = (shared or "").strip()
    notes = notes.strip()
    if not notes or notes in shared:
        return shared
    return (f"{shared}\n\n{notes}" if shared else notes)[:MAX_WIKI_DESCRIPTION_LENGTH]


def answer_notes_offer(offer: NotesOffer, profile: Profile, *, accept: bool) -> bool:
    """
    Record the owner's answer; on accept, copy their own tier-0 notes into the shared wiki.

    Returns:
        Whether the offer was pending and belonged to *profile*.
    """
    from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit

    if offer.profile_id != profile.pk or offer.status != NotesOfferStatus.PENDING:
        return False
    if accept:
        source = Wiki.objects.filter(location_id=offer.source_location_id).first() if offer.source_location_id else None
        shared = Wiki.objects.filter(place_id=offer.place_id).first()
        if source is not None and shared is not None and SharedPlaceMember.objects.filter(place_id=offer.place_id, profile=profile).exists():
            apply_wiki_edit(shared, profile, {"description": merged_description(shared.description, source.description or "")})
    offer.status = NotesOfferStatus.ACCEPTED if accept else NotesOfferStatus.DECLINED
    offer.answered_at = timezone.now()
    offer.save(update_fields=["status", "answered_at", "updated"])
    return True
