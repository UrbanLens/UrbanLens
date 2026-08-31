"""Shared visibility gate for wiki-scoped views.

Wikis are opt-in shared: a profile may see (or act on) a Wiki only for a place
they have pinned themselves. Every wiki-scoped controller must resolve its
Location/Wiki through :func:`resolve_visible_wiki` (or check
:func:`location_visible_to` directly) so that a ``location_slug`` for a place
the profile hasn't pinned is indistinguishable from one that doesn't exist at
all - otherwise the slug becomes an oracle for discovering which places other
users have pinned, which undermines the whole point of the site.

**The rule, in full.** Access is evaluated over *access domains*, not
geometry - see
:class:`~urbanlens.dashboard.models.place.model.PlaceRelation`. A domain is a
parcel plus everything ``PART_OF`` it, and it is indivisible: a pin anywhere
in it grants every wiki in it, in either direction. Splitting a property into
its 124 buildings is an organisational act and must not change who can see
what.

Only ``MEMBER_OF`` edges gate anything. Their parent - a campus that was split
into several parcels, or a site that spans several - is reachable only by
holding access to *every* member, because such a parent's knowledge genuinely
exceeds any one child's. Grants
(:class:`~urbanlens.dashboard.models.place.model.PlaceAccessGrant`) cover the
one case the rule cannot: users who already held access when the structure
changed underneath them.

Two properties worth stating outright:

- **Nothing user-drawn is ever consulted.** Access reads ``Place.geometry``
  (provider chain and boundary voting only) and grants. The ``Boundary`` table,
  where every user- and community-drawn shape lives, is not read here at all -
  so the anti-gaming invariant is structural, not a filter to remember.
- **Placeless locations still work.** A coordinate no provider knows has no
  place and therefore no domain; its wiki stays reachable by an exact-Location
  pin, exactly as before places existed.

**Grandfathering is permanent, not a recomputed fallback.** Two more cases
snapshot a :class:`~urbanlens.dashboard.models.place.model.PlaceAccessGrant`
at the moment they happen, rather than every request recomputing whether they
still hold:

- **Earning a split-derived family.** ``services.places.splits.process_split``
  snapshots every prior holder of an undivided parcel across the whole family
  it splits into. A profile who was never a prior holder but later earns the
  same aggregate the ordinary way - by independently pinning every one of its
  current successors - gets the identical snapshot at the moment that
  happens (:func:`_snapshot_earned_split_families`, called from
  :func:`_domains_given_pins`). Either way, from that moment the whole family
  stays visible even if every qualifying pin is later moved or deleted - an
  *organic* multi-parcel site (never split) gets no such snapshot and stays
  governed by the strict hold-every-member-every-time rule.
- **Engaging with a wiki.** A profile who actually viewed a wiki, or shared
  content to it, while they held access keeps that access permanently -
  see :func:`resolve_visible_wiki` and
  :meth:`~urbanlens.dashboard.services.wiki.wiki_share.WikiShareService.share_from_pin`.
  One who never engaged with it loses access the moment their last qualifying
  pin is gone, exactly as before this existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import Http404
from django.shortcuts import get_object_or_404

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

#: Ceiling on earned-access fixpoint rounds. Each round can only unlock
#: aggregates one lineage tier higher, and real lineage is two or three deep;
#: this exists so corrupted lineage degrades into a logged error rather than a
#: spinning request.
MAX_EARNING_ROUNDS = 16


def _point_is_at(point, location: Location) -> bool:
    """Whether *point* resolves to *location*'s own coordinates.

    Compared at the precision ``Location`` stores rather than as raw floats,
    because that rounding is what decides which Location row a point lands on.

    Args:
        point: The point being tested.
        location: The Location to compare against.

    Returns:
        Whether the two name the same stored coordinate pair.
    """
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    if location.latitude is None or location.longitude is None:
        return False
    return quantize_coordinate(point.y, "latitude") == location.latitude and quantize_coordinate(point.x, "longitude") == location.longitude


def _earn_aggregates(domains: set[int]) -> set[int]:
    """Extend a domain set with every aggregate its members fully cover.

    Repeated to a fixpoint, because earning one tier can complete the member
    set of the tier above it: with campus -> {A, B} and A -> {A1, A2}, holding
    A1 and A2 earns A, which together with B then earns the campus. A user
    proves knowledge of the whole by proving knowledge of every part.

    Args:
        domains: Domain roots already accessible. Not mutated.

    Returns:
        The closure, including everything in ``domains``.
    """
    from urbanlens.dashboard.models.place.model import Place, PlaceRelation

    aggregate_roots = dict(Place.objects.filter(is_aggregate=True).values_list("pk", "domain_root_id"))
    if not aggregate_roots:
        return set(domains)

    members: dict[int, set[int]] = {}
    for parent_id, child_root in Place.objects.filter(parent_id__in=list(aggregate_roots), parent_relation=PlaceRelation.MEMBER_OF).values_list("parent_id", "domain_root_id"):
        members.setdefault(parent_id, set()).add(child_root)

    earned = set(domains)
    for _ in range(MAX_EARNING_ROUNDS):
        added = False
        for aggregate_id, member_roots in members.items():
            root = aggregate_roots[aggregate_id]
            if root in earned or not member_roots:
                continue
            if member_roots <= earned:
                earned.add(root)
                added = True
        if not added:
            return earned
    return earned


def _snapshot_earned_split_families(profile: Profile, domain_ids: set[int]) -> None:
    """Permanently grant a profile any split-derived family they've just earned.

    ``_earn_aggregates`` adds a domain to the earned set, on every call,
    whenever a profile currently holds every one of its ``MEMBER_OF``
    children - that computation is cheap and correct, but purely transient:
    lose one child and the aggregate drops right back out. For a
    *split-derived* aggregate (``PlaceStatus.SUPERSEDED``) that transience is
    wrong: a profile who has proved full knowledge of a family that used to
    be one parcel deserves the same permanent grant a prior holder got at
    split time (see ``services.places.splits.process_split``), not a status
    that evaporates the moment they drop one pin.

    An *organic* multi-parcel site (``PlaceStatus.CURRENT``, never split) is
    deliberately excluded - it was never one thing, so there is nothing to be
    grandfathered back into, and it keeps the ordinary hold-every-member rule.

    Idempotent and cheap on the common case: the aggregate lookup is the only
    query when a profile's earned domains contain no split-derived aggregate,
    and a profile who already holds the family's grant is skipped before any
    write is attempted.

    Args:
        profile: The profile whose earned domains were just computed.
        domain_ids: The full earned closure from :func:`_earn_aggregates`.
    """
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

    The one implementation of the access rule. :func:`accessible_domain_ids`
    passes a profile's real pins; the pin-move preview passes a hypothetical
    set (every pin except the one being moved, plus that pin's proposed point),
    so the preview can never drift from the rule actually enforced.

    Args:
        pins: A ``Pin`` queryset standing in for the viewer's pins.
        profile: The viewer, for grant lookup; None skips grants.
        extra_point: An additional point to treat as pinned, for previewing a
            move before it happens.

    Returns:
        Set of ``Place.domain_root_id`` values.
    """
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
        Set of ``Place.domain_root_id`` values, empty for a profile with no
        placed pins and no grants.
    """
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
        Whether that set of pins grants visibility of *location*'s wiki.
    """
    if pins.filter(location=location).exists():
        return True

    # A previewed move that lands on this Location's own coordinates keeps the
    # exact-match grant above, which *pins* alone can't show because the pin
    # being moved is deliberately excluded from it. Checked before the place
    # lookup: a coordinate no provider knows has no place at all, so exact
    # match is its only route and skipping this would report a stay-put move
    # as losing access.
    if extra_point is not None and _point_is_at(extra_point, location):
        return True

    place = location.place if location.place_id else None
    if place is None or place.domain_root_id is None:
        return False
    return place.domain_root_id in _domains_given_pins(pins, profile, extra_point=extra_point)


def location_visible_to(location: Location, profile: Profile) -> bool:
    """Whether *profile* has a pin at *location*, or anywhere in its access domain.

    A pin at the exact same ``Location`` row always qualifies. Otherwise the
    profile qualifies when any of their pins resolves onto the same real-world
    thing - the parcel, or any building on it - which is what makes two users
    who pinned the same property metres apart share its wiki without either
    having to pin the other's exact coordinate.

    Args:
        location: The Location to check.
        profile: The viewing profile.

    Returns:
        Whether the profile can see this location's wiki.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return _visible_given_pins(location, Pin.objects.filter(profile=profile), profile)


def visible_wiki_location_ids(profile: Profile) -> set[int]:
    """Location ids of every Wiki visible to *profile*.

    The set-shaped counterpart to :func:`location_visible_to`, for callers
    that need the whole visible set at once (e.g. the custom-field REFERENCE
    picker's wiki queryset). Entirely database-side now that access is a
    domain lookup rather than a containment scan.

    Args:
        profile: The viewing profile.

    Returns:
        Set of Location primary keys whose Wiki is visible to *profile*.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

    direct_ids = set(Pin.objects.filter(profile=profile).values_list("location_id", flat=True))
    domains = accessible_domain_ids(profile)
    if not domains:
        return direct_ids
    return direct_ids | set(Location.objects.filter(wiki__isnull=False, place__domain_root_id__in=domains).values_list("pk", flat=True))


#: Instance attribute the per-request memoization hangs on.
_CACHE_ATTR = "_ul_visible_wiki_location_ids"


def visible_wiki_location_ids_cached(profile: Profile) -> set[int]:
    """:func:`visible_wiki_location_ids`, memoised on the profile instance.

    One global search fans out to eleven providers, four of which need the
    viewer's wiki reach. Recomputing it per provider costs the pin lookup, the
    aggregate fixpoint and the location query four times over for an answer that
    cannot change mid-request.

    Cached on the instance rather than in a module-level dict deliberately: a
    ``Profile`` is loaded fresh per request, so the entry cannot outlive the
    request that made it, and nothing has to invalidate it when a pin moves.
    Pass the *same* instance to every caller that should share the answer.

    Args:
        profile: The viewing profile.

    Returns:
        Set of Location primary keys whose Wiki is visible to *profile*.
    """
    cached = getattr(profile, _CACHE_ATTR, None)
    if cached is None:
        cached = visible_wiki_location_ids(profile)
        # setattr rather than a direct assignment: the attribute is not declared
        # on Profile, and assigning it directly is an error django-stubs is
        # right to flag. Memoizing on the instance is still the point - a
        # Profile is loaded fresh per request, so the entry cannot outlive one.
        setattr(profile, _CACHE_ATTR, cached)
    return cached


def wikis_hidden_by_pin_move(pin: Pin, latitude: float, longitude: float) -> list[Wiki]:
    """Wikis the owner can see now but would lose by moving *pin* to this point.

    Only wikis this pin is actually keeping visible are returned: one the owner
    also reaches through another of their own pins is never listed, and neither
    is one they can't see in the first place. That makes an empty result the
    normal case, so callers can treat a non-empty one as genuinely worth
    interrupting the user for.

    The candidate set is everything the owner currently sees, not just what
    this pin's own domain covers - moving the last pin out of one member of an
    aggregate un-earns that aggregate too, and a narrower candidate set would
    silently miss it.

    Advisory only - it previews the rule rather than enforcing it. Access
    itself is always decided by :func:`location_visible_to` at read time, so a
    stale or slightly-off preview can never grant access, only mis-warn about
    it.

    Args:
        pin: The pin about to move.
        latitude: Proposed new latitude.
        longitude: Proposed new longitude.

    Returns:
        The affected wikis, each with its ``location`` selected. Empty when the
        move costs the owner nothing.
    """
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    if pin.location_id is None or pin.location.point is None:
        return []

    new_point = Point(float(longitude), float(latitude), srid=4326)
    profile = pin.profile

    visible_ids = visible_wiki_location_ids(profile)
    if not visible_ids:
        return []
    candidates = list(Wiki.objects.filter(location_id__in=visible_ids).select_related("location", "location__place"))
    if not candidates:
        return []

    remaining = Pin.objects.filter(profile=profile).exclude(pk=pin.pk)
    return [wiki for wiki in candidates if not _visible_given_pins(wiki.location, remaining, profile, extra_point=new_point)]


def visible_parent_wiki(wiki: Wiki, profile: Profile) -> Wiki | None:
    """The wiki's parent, but only when *profile* may actually open it.

    Rendering a breadcrumb to a wiki that 404s is itself a disclosure: it
    confirms a place exists that the viewer has not earned. Within one access
    domain the parent is always visible, so this only ever withholds across a
    ``MEMBER_OF`` edge - the campus a viewer holds one parcel of.

    Args:
        wiki: The wiki being rendered.
        profile: The viewing profile.

    Returns:
        The parent wiki, or None when there isn't one or it must stay hidden.
    """
    if not wiki.parent_wiki_id:
        return None
    parent = wiki.parent_wiki
    if parent is None or parent.location_id is None:
        return None
    return parent if location_visible_to(parent.location, profile) else None


def resolve_visible_wiki(request: HttpRequest, location_slug: str) -> tuple[Location, Wiki, Profile]:
    """Resolve a Location and its Wiki, 404ing unless the requester can see it.

    A location with no wiki yet, a location_slug that doesn't exist at all,
    and a real wiki the requester hasn't earned all raise the identical
    ``Http404`` - deliberately indistinguishable, so guessing slugs can never
    reveal which locations other users have pinned.

    The wiki is found through the Location's *place*, not only through the
    Location itself, so everyone who pinned one property reaches the same page
    from their own slug. Without that, the second person to pin a parcel -
    metres from the first, but at their own coordinate - would get a 404 on a
    page they can plainly see.

    Args:
        request: The current request (used for the requesting profile).
        location_slug: Slug of the Location whose Wiki is being resolved.

    Returns:
        Tuple of (Location, Wiki, requester's Profile). The Wiki may be a
        **concealed projection** - a real Wiki instance, with the real primary
        key, carrying only the field values this viewer is entitled to. It
        refuses ``save()``/``delete()``; a write path must re-fetch the row or
        use ``queryset.update()``. Related managers on it are *not* filtered -
        rows are concealed separately by ``concealment.conceal_rows``.

    Raises:
        Http404: The location doesn't exist, has no wiki, or the requester
            can't see it.
    """
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

    # Grandfathers a profile who actually viewed this wiki while they held
    # access - see the module docstring's "Engaging with a wiki". Every
    # wiki-scoped controller resolves through here, so this one call covers
    # viewing and (since editing/commenting/sharing surfaces resolve the same
    # way before they write) most content-sharing paths too; the one that
    # doesn't - WikiShareService.share_from_pin - records it separately.
    if location.place_id is not None:
        from urbanlens.dashboard.models.place.model import PlaceAccessGrant

        PlaceAccessGrant.objects.record_engagement(profile, location.place)

    # Concealment is applied *here*, at the one place every wiki-scoped surface
    # already funnels through - 56 controller call sites plus the external API's
    # 32 handlers, all of which get it without knowing it exists.
    #
    # It was previously applied per call site, and that is why an adversarial
    # review found roughly two dozen surfaces still serving real values: the
    # ones somebody remembered were right and the rest were untouched. A rule
    # kept in one place cannot be forgotten at the others.
    #
    # This narrows what a wiki *shows*. It never widens who can reach one -
    # that decision is the place-domain rule above, and the two stay separate so
    # concealment can never become an access-control bypass.
    from urbanlens.dashboard.services.wiki.concealment import conceal_wiki

    return location, conceal_wiki(wiki, profile), profile
