"""Shared visibility gate for wiki-scoped views.
Wikis are opt-in shared: a profile may see (or act on) a Wiki only for a place they have pinned themselves."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

#: Ceiling on earned-access fixpoint rounds. The walk resolves exactly one
#: lineage tier per round and real lineage is two or three deep, so reaching
#: this means the lineage is corrupt (most likely a cycle). Hitting it returns
#: what was earned so far - which is less than the truth, never more - and says
#: so in the log, because an access check that quietly stops early is a wrong
#: answer nobody can find later.
MAX_EARNING_ROUNDS = 16


def _point_is_at(point, location: Location) -> bool:
    """Whether *point* resolves to *location*'s own coordinates.
    Compared at the precision ``Location`` stores rather than as raw floats, because that rounding is what decides which Location row a point lands on.

    Args:
        point: The point being tested.
        location: The Location to compare against.

    Returns:
        Whether the two name the same stored coordinate pair."""
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    if location.latitude is None or location.longitude is None:
        return False
    return quantize_coordinate(point.y, "latitude") == location.latitude and quantize_coordinate(point.x, "longitude") == location.longitude


def _earn_aggregates(domains: set[int]) -> set[int]:
    """Extend a domain set with every aggregate its members fully cover.
    Repeated to a fixpoint, because earning one tier can complete the member set of the tier above it: with campus -> {A, B} and A -> {A1, A2}, holding A1 and A2 earns A, which together with B then earns the campus.

    Walks up from the domains already held rather than sweeping the site's
    aggregates: an aggregate is earned only when *every* member is held, so one
    with no held member can never be earned and never needs reading. That keeps
    the cost of an access check - which runs on every wiki page and every
    non-owned media fetch - proportional to the viewer's own reach instead of to
    how many places everyone else has split up.

    Args:
        domains: Domain roots already accessible.

    Returns:
        The closure, including everything in ``domains``."""
    from urbanlens.dashboard.models.place.model import Place, PlaceRelation

    earned = set(domains)
    # A null-rooted place is unreachable by definition (see place_visible_to),
    # so it can neither be a frontier nor complete an aggregate.
    frontier = {root for root in earned if root is not None}

    for _ in range(MAX_EARNING_ROUNDS):
        if not frontier:
            return earned
        candidate_ids = set(Place.objects.filter(parent_relation=PlaceRelation.MEMBER_OF, parent__is_aggregate=True, domain_root_id__in=frontier).values_list("parent_id", flat=True))
        if not candidate_ids:
            return earned

        members: dict[int, set[int]] = {}
        for parent_id, child_root in Place.objects.filter(parent_id__in=candidate_ids, parent_relation=PlaceRelation.MEMBER_OF).values_list("parent_id", "domain_root_id"):
            members.setdefault(parent_id, set()).add(child_root)
        covered = [parent_id for parent_id, member_roots in members.items() if member_roots <= earned]
        if not covered:
            return earned

        frontier = {root for root in Place.objects.filter(pk__in=covered).values_list("domain_root_id", flat=True) if root is not None and root not in earned}
        earned |= frontier

    logger.error(
        "Earned-access walk hit its %s-round ceiling with %s domains still expanding; place lineage is probably cyclic. Returning what was earned so far.",
        MAX_EARNING_ROUNDS,
        len(frontier),
    )
    return earned


def _snapshot_earned_split_families(profile: Profile, domain_ids: set[int]) -> None:
    """Permanently grant a profile any split-derived family they've just earned.
    An *organic* multi-parcel site (``PlaceStatus.CURRENT``, never split) is deliberately excluded - it was never one thing, so there is nothing to be grandfathered back into, and it keeps the ordinary hold-every-member rule.

    Args:
        profile: The profile whose earned domains were just computed.
        domain_ids: The full earned closure from :func:`_earn_aggregates`."""
    from urbanlens.dashboard.models.place.model import Place, PlaceAccessGrant, PlaceStatus

    if not domain_ids:
        return
    aggregate_ids = set(Place.objects.filter(pk__in=domain_ids, is_aggregate=True, status=PlaceStatus.SUPERSEDED).values_list("pk", flat=True))
    if not aggregate_ids:
        return
    already_granted = set(PlaceAccessGrant.objects.filter(profile=profile, place_id__in=aggregate_ids).values_list("place_id", flat=True))
    newly_earned = aggregate_ids - already_granted
    if not newly_earned:
        return
    for aggregate in Place.objects.filter(pk__in=newly_earned):
        PlaceAccessGrant.objects.snapshot_family([profile.pk], aggregate)


def _domains_given_pins(pins, profile: Profile | None, *, extra_point=None) -> set[int]:
    """Every access domain the given pins (plus any grants) reach.

    Args:
        pins: A ``Pin`` queryset standing in for the viewer's pins.
        profile: The viewer, for grant lookup; None skips grants.
        extra_point: An additional point to treat as pinned, for previewing a move before it happens.

    Returns:
        Set of ``Place.domain_root_id`` values."""
    from urbanlens.dashboard.models.place.model import Place, PlaceAccessGrant

    domains = set(pins.filter(location__place__isnull=False).values_list("location__place__domain_root_id", flat=True))

    if extra_point is not None:
        moved_to = Place.objects.resolve_for_point(extra_point.y, extra_point.x)
        if moved_to is not None:
            domains.add(moved_to.domain_root_id)

    if profile is not None:
        domains |= PlaceAccessGrant.objects.granted_domain_ids(profile)

    earned = _earn_aggregates(domains)

    # Only for a real (non-preview) computation of a real profile's own
    # access: a hypothetical pin-move preview must never snapshot a
    # permanent grant off of a move that hasn't happened.
    if profile is not None and extra_point is None:
        _snapshot_earned_split_families(profile, earned)

    return earned


def accessible_domain_ids(profile: Profile) -> set[int]:
    """Every access domain *profile* can reach.

    Args:
        profile: The viewing profile.

    Returns:
        Set of ``Place.domain_root_id`` values, empty for a profile with no placed pins and no grants."""
    from urbanlens.dashboard.models.pin.model import Pin

    return _domains_given_pins(Pin.objects.filter(profile=profile), profile)


def place_visible_to(place: Place | None, profile: Profile) -> bool:
    """Whether *profile* can reach a place's access domain.

    Args:
        place: The place to check; None is never visible on its own.
        profile: The viewing profile.

    Returns:
        Whether the profile holds the domain.
    """
    if place is None or place.domain_root_id is None:
        return False
    return place.domain_root_id in accessible_domain_ids(profile)


def _visible_given_pins(location: Location, pins, profile: Profile | None, *, extra_point=None) -> bool:
    """Whether *location*'s wiki is visible to the owner of *pins*.

    Args:
        location: The Location whose wiki is being tested.
        pins: A ``Pin`` queryset standing in for the viewer's pins.
        profile: The viewer, for grant lookup; None skips grants.
        extra_point: An additional point to treat as pinned.

    Returns:
        Whether that set of pins grants visibility of *location*'s wiki."""
    if pins.filter(location=location).exists():
        return True

    # A previewed move that lands on this Location's own coordinates keeps the exact-match grant
    # above, which *pins* alone can't show because the pin being moved is deliberately excluded from
    # it.
    # Checked before the place lookup: a coordinate no provider knows has no place at all, so exact
    if extra_point is not None and _point_is_at(extra_point, location):
        return True

    place = location.place if location.place_id else None
    if place is None or place.domain_root_id is None:
        return False
    return place.domain_root_id in _domains_given_pins(pins, profile, extra_point=extra_point)


def location_visible_to(location: Location, profile: Profile) -> bool:
    """Whether *profile* has a pin at *location*, or anywhere in its access domain.

    Args:
        location: The Location to check.
        profile: The viewing profile.

    Returns:
        Whether the profile can see this location's wiki."""
    from urbanlens.dashboard.models.pin.model import Pin

    return _visible_given_pins(location, Pin.objects.filter(profile=profile), profile)


type VisibleWikiLocations = QuerySet[Pin, int] | QuerySet[Location, int]


def visible_wiki_locations(profile: Profile) -> VisibleWikiLocations:
    """Location ids of every Wiki visible to *profile*, as a subquery to filter with.

    The ids stay in the database, so a query filtering on them costs the same to build and plan however many
    places the viewer has pinned.

    Args:
        profile: The viewing profile.

    Returns:
        A one-column queryset of Location primary keys whose Wiki is visible to *profile*."""
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

    pinned = Pin.objects.filter(profile=profile, location_id__isnull=False).values_list("location_id", flat=True)
    domains = accessible_domain_ids(profile)
    if not domains:
        return pinned
    return Location.objects.filter(Q(pk__in=pinned) | Q(wiki__isnull=False, place__domain_root_id__in=domains)).values_list("pk", flat=True)


def visible_wiki_location_ids(profile: Profile) -> set[int]:
    """:func:`visible_wiki_locations` read into a set, for membership tests.

    Args:
        profile: The viewing profile.

    Returns:
        Set of Location primary keys whose Wiki is visible to *profile*."""
    return set(visible_wiki_locations(profile))


#: Instance attribute the per-request memoization hangs on.
_CACHE_ATTR = "_ul_visible_wiki_locations"


def visible_wiki_locations_cached(profile: Profile) -> VisibleWikiLocations:
    """:func:`visible_wiki_locations`, memoised on the profile instance.
    Cached on the instance rather than in a module-level dict deliberately: a ``Profile`` is loaded fresh per request, so the entry cannot outlive the request that made it. What it saves is the domain lookup; the pinned half is read by each query that filters on it.

    Args:
        profile: The viewing profile.

    Returns:
        A one-column queryset of Location primary keys whose Wiki is visible to *profile*."""
    cached = getattr(profile, _CACHE_ATTR, None)
    if cached is None:
        cached = visible_wiki_locations(profile)
        # setattr rather than a direct assignment: the attribute is not declared on Profile, and
        # assigning it directly is an error django-stubs is right to flag.
        setattr(profile, _CACHE_ATTR, cached)
    return cached


def visible_wiki_locations_if_primed(profile: Profile) -> VisibleWikiLocations | None:
    """The memoised subquery, but only when somebody has already asked for it.

    Args:
        profile: The viewing profile.

    Returns:
        The primed subquery, or None when nothing has primed it."""
    return getattr(profile, _CACHE_ATTR, None)


def wikis_hidden_by_pin_move(pin: Pin, latitude: float, longitude: float) -> list[Wiki]:
    """Wikis the owner can see now but would lose by moving *pin* to this point.
    Only wikis this pin is actually keeping visible are returned: one the owner also reaches through another of their own pins is never listed, and neither is one they can't see in the first place.

    Args:
        pin: The pin about to move.
        latitude: Proposed new latitude.
        longitude: Proposed new longitude.

    Returns:
        The affected wikis, each with its ``location`` selected."""
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    if pin.location_id is None or pin.location.point is None:
        return []

    new_point = Point(float(longitude), float(latitude), srid=4326)
    profile = pin.profile

    candidates = list(Wiki.objects.filter(location_id__in=visible_wiki_locations(profile)).select_related("location", "location__place"))
    if not candidates:
        return []

    remaining = Pin.objects.filter(profile=profile).exclude(pk=pin.pk)
    return [wiki for wiki in candidates if not _visible_given_pins(wiki.location, remaining, profile, extra_point=new_point)]


def visible_parent_wiki(wiki: Wiki, profile: Profile) -> Wiki | None:
    """The wiki's parent, but only when *profile* may actually open it.

    Args:
        wiki: The wiki being rendered.
        profile: The viewing profile.

    Returns:
        The parent wiki, or None when there isn't one or it must stay hidden."""
    if not wiki.parent_wiki_id:
        return None
    parent = wiki.parent_wiki
    if parent is None or parent.location_id is None:
        return None
    return parent if location_visible_to(parent.location, profile) else None


def resolve_visible_wiki(request: HttpRequest, location_slug: str) -> tuple[Location, Wiki, Profile]:
    """Resolve a Location and its Wiki, 404ing unless the requester can see it.

    Args:
        request: The current request (used for the requesting profile).
        location_slug: Slug of the Location whose Wiki is being resolved.

    Returns:
        Tuple of (Location, Wiki, requester's Profile).

    Raises:
        Http404: The location doesn't exist, has no wiki, or the requester can't see it."""
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

    location = get_object_or_404(Location.objects.slug_or_uuid(location_slug).select_related("place"))
    wiki = Wiki.objects.get_for_location(location)
    if wiki is None:
        raise Http404
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not location_visible_to(location, profile):
        raise Http404

    # Grandfathers a profile who actually viewed this wiki while they held access - see the module
    # docstring's "Engaging with a wiki".
    # Every wiki-scoped controller resolves through here, so this one call covers viewing and (since
    # editing/commenting/sharing surfaces resolve the same way before they write) most
    if location.place_id is not None:
        from urbanlens.dashboard.models.place.model import PlaceAccessGrant

        PlaceAccessGrant.objects.record_engagement(profile, location.place)

    # Concealment is applied *here*, at the one place every wiki-scoped surface already funnels
    # through - 56 controller call sites plus the external API's 32 handlers, all of which get it
    # without knowing it exists.
    # A rule kept in one place cannot be forgotten at the others.
    from urbanlens.dashboard.services.wiki.concealment import conceal_wiki

    return location, conceal_wiki(wiki, profile), profile
