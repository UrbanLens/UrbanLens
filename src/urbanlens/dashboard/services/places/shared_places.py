"""
Shared placeless places: provisional and anchored tiers (ADR35 section 2, UL-367).

Everything here runs in Celery (``tasks.evaluate_shared_places_for_pin`` and the nightly sweep), never in a request or page view, so
no response can reveal whether a tier formed, when, or whose pin took part. The only thing a request ever does is the owner accepting or
declining their own :class:`NotesOffer` (:func:`answer_notes_offer`).

A synthetic place never resolves onto a ``Location`` (``PlaceQuerySet.resolvable`` excludes it). Access to its wiki is explicit
``SharedPlaceMember`` membership, read by ``services.wiki.wiki_access``.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.gis.db.models.functions import GeoHash
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.contrib.gis.measure import D
from django.db import connection, transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import STRONG_EVIDENCE, Place, PlaceEvidence, PlaceKind, PlaceRelation, PlaceStatus
from urbanlens.dashboard.models.place.shared import NotesOffer, NotesOfferStatus, SharedPlaceMember
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH
from urbanlens.dashboard.services.geo.area import buffer_meters
from urbanlens.dashboard.services.locations.site_scope import meters_between
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.trust.checks import are_independent, is_real_user
from urbanlens.dashboard.services.trust.signals import CELL_PRECISION

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

SYNTHETIC_EVIDENCE = (PlaceEvidence.PROVISIONAL, PlaceEvidence.ANCHORED)
#: Serialises place creation so two evaluations of one cluster cannot each make a place.
_CREATION_LOCK_KEY = 367_001
#: How far the hub Location is nudged off the anchor to avoid an existing Location at the same 6-decimal coordinate.
_HUB_NUDGE_DEGREES = 0.000001
_HUB_NUDGE_ATTEMPTS = 60


def _radius(evidence: str) -> float:
    return settings.UL_SHARED_PLACE_ANCHORED_RADIUS_M if evidence == PlaceEvidence.ANCHORED else settings.UL_SHARED_PLACE_PROVISIONAL_RADIUS_M


def _distance(a: Point, b: Point) -> float:
    return meters_between(a.y, a.x, b.y, b.x)


def pin_has_matured(pin: Pin) -> bool:
    """Whether the pin is old enough, or has a photo-verified visit; a fresh pin is cheap to scatter."""
    if pin.created <= timezone.now() - timedelta(days=settings.UL_SHARED_PLACE_MIN_PIN_AGE_DAYS):
        return True
    return PinVisit.objects.filter(pin=pin, tentative=False, source=VisitSource.PHOTO).exists()


def resolved_official_place(location: Location) -> Place | None:
    """The place the coordinate resolves onto today, or None."""
    return Place.objects.resolve_for_point(float(location.latitude), float(location.longitude))


def pin_is_placeless(location: Location) -> bool:
    """Whether nothing with a building outline sits under the pin; a parcel alone does not count as a place for the pin."""
    resolved = resolved_official_place(location)
    return resolved is None or resolved.kind != PlaceKind.BUILDING


def _pin_qualifies(pin: Pin) -> bool:
    """The pin-level gates: community on, a location, nothing building-like under it, mature, and a real user behind it."""
    if pin.location_id is None or not pin.profile.community_enabled:
        return False
    return pin_is_placeless(pin.location) and pin_has_matured(pin) and is_real_user(pin.profile)


def _recent_joins(profile: Profile) -> int:
    since = timezone.now() - timedelta(hours=settings.UL_SHARED_PLACE_JOIN_WINDOW_HOURS)
    return SharedPlaceMember.objects.filter(profile=profile, created__gte=since).count()


def _under_join_limit(profile: Profile) -> bool:
    """Per-account join rate limit. A profile over it is skipped and logged, which is how a grid of probe pins is flagged."""
    if _recent_joins(profile) >= settings.UL_SHARED_PLACE_JOIN_LIMIT:
        logger.warning("Shared-place join throttled for profile %s (over %s joins in %sh); flagged for review", profile.pk, settings.UL_SHARED_PLACE_JOIN_LIMIT, settings.UL_SHARED_PLACE_JOIN_WINDOW_HOURS)
        return False
    return True


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
    nearby = GooglePlace.objects.filter(latitude__range=(point.y - span, point.y + span), longitude__range=(point.x - span * 2, point.x + span * 2))
    best: tuple[float, Point] | None = None
    for ref in nearby:
        ref_point = Point(float(ref.longitude), float(ref.latitude), srid=4326)
        gap = _distance(ref_point, point)
        if gap <= reach and (best is None or gap < best[0]):
            best = (gap, ref_point)
    return best[1] if best else None


def _pins_near(point: Point, radius: float, *, excluding: Profile) -> list[Pin]:
    pins = Pin.objects.filter(location__point__dwithin=(point, D(m=radius))).exclude(profile=excluding).select_related("location", "profile").order_by("created", "pk")
    return [pin for pin in pins if _distance(pin.location.point, point) <= radius]


def evaluate_pin(pin_id: int) -> Place | None:
    """
    Run the trust checks and tier evaluation for one pin. Celery only.

    Joins the pin's profile to an existing synthetic place by distance to its single anchor, or forms a new anchored then provisional
    place from a qualifying, independent pair of profiles.

    Returns:
        The place the profile joined or formed, else None. The return value never leaves the worker.
    """
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    if pin is None or pin.location_id is None:
        return None
    revalidate_memberships(pin.profile)
    if not _pin_qualifies(pin) or not _under_join_limit(pin.profile):
        return None
    point = pin.location.point

    existing = _nearest_synthetic_place(point)
    if existing is not None:
        return _join(existing, pin)

    reference = _nearest_reference(point)
    if reference is not None:
        formed = _form(pin, reference, PlaceEvidence.ANCHORED)
        if formed is not None:
            return formed
    return _form(pin, None, PlaceEvidence.PROVISIONAL)


def _form(pin: Pin, reference: Point | None, evidence: str) -> Place | None:
    """Look for a qualifying independent partner near *pin* (or near the public *reference*) and create the place."""
    centre = reference if reference is not None else pin.location.point
    radius = _radius(evidence)
    for other in _pins_near(centre, radius, excluding=pin.profile):
        if not _pin_qualifies(other) or not _under_join_limit(other.profile):
            continue
        if reference is None and _distance(pin.location.point, other.location.point) > radius:
            continue
        if not are_independent(pin.profile, other.profile, exclude_cell=_cell_of(pin.location)):
            continue
        earlier = min((pin, other), key=lambda candidate: (candidate.created, candidate.pk))
        anchor = reference if reference is not None else earlier.location.point
        return _create_place(anchor, evidence, [pin, other])
    return None


def _join(place: Place, pin: Pin) -> Place | None:
    """Admit the pin's profile by distance to the anchor alone, never pairwise."""
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


def _hub_location(anchor: Point) -> Location:
    """A Location of its own beside the anchor, because an existing Location at the anchor already holds a tier-0 wiki."""
    for step in range(1, _HUB_NUDGE_ATTEMPTS):
        location, created = Location.objects.get_exact_or_create(anchor.y, anchor.x + step * _HUB_NUDGE_DEGREES)
        if created or not (location.pins.exists() or Wiki.objects.filter(location=location).exists()):
            return location
    raise RuntimeError("no free hub coordinate beside the anchor")


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
        hub = _hub_location(anchor)
        wiki = Wiki.objects.create(location=hub, place=place, name="Shared place")
        for pin in pins:
            SharedPlaceMember.objects.get_or_create(place=place, profile=pin.profile)
        from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting

        reconcile_wiki_nesting(wiki)
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
