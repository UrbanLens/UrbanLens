"""Shared visibility gate for wiki-scoped views.

Wikis are opt-in shared: a profile may see (or act on) a Wiki only for a
Location they have pinned themselves. Every wiki-scoped controller must
resolve its Location/Wiki through :func:`resolve_visible_wiki` (or check
:func:`location_visible_to` directly) so that a ``location_slug`` for a place
the profile hasn't pinned is indistinguishable from one that doesn't exist at
all - otherwise the slug becomes an oracle for discovering which places other
users have pinned, which undermines the whole point of the site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


def _location_default_polygons(location: Location) -> list:
    """The official boundary polygons for *location* - and only those.

    Restricted to location-default ``Boundary`` rows (no owning pin, wiki, or
    profile, and no per-provider ``source``) and to ``generated_polygon``, which
    only the provider chain and boundary voting ever write. The user-editable
    ``polygon`` on the same row is display-only: consulting it would let anyone
    draw a boundary around a region and inherit every wiki inside it.

    Args:
        location: The Location whose official boundaries are wanted.

    Returns:
        Zero or more polygons - typically the property boundary plus, when
        known, the building footprint.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary

    return list(
        Boundary.objects.filter(location=location, pin__isnull=True, wiki__isnull=True, profile__isnull=True, source="").exclude(generated_polygon__isnull=True).values_list("generated_polygon", flat=True),
    )


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


def _visible_given_pins(location: Location, pins, *, extra_point=None) -> bool:
    """Whether *location*'s wiki is visible to the owner of *pins*.

    The one implementation of the visibility rule. :func:`location_visible_to`
    passes a profile's real pins; the pin-move preview passes a hypothetical set
    (every pin except the one being moved, plus that pin's proposed point), so
    the preview can never drift from the rule actually enforced.

    Args:
        location: The Location whose wiki is being tested.
        pins: A ``Pin`` queryset standing in for the viewer's pins.
        extra_point: An additional point to treat as pinned, for previewing a
            move before it happens.

    Returns:
        Whether that set of pins grants visibility of *location*'s wiki.
    """
    if pins.filter(location=location).exists():
        return True

    # A previewed move that lands on this Location's own coordinates keeps the
    # exact-match grant above, which *pins* alone can't show because the pin
    # being moved is deliberately excluded from it. Checked before the polygon
    # guard below: a place whose boundary lookup found nothing (common for
    # obscure rural spots) is reachable only by exact match, so skipping this
    # would report a stay-put move as losing access.
    if extra_point is not None and _point_is_at(extra_point, location):
        return True

    polygons = _location_default_polygons(location)
    if not polygons:
        return False

    if extra_point is not None and any(polygon.contains(extra_point) for polygon in polygons):
        return True

    # The polygons are read into Python (a handful of location-default boundary
    # rows per wiki - property plus building) but the containment test itself
    # runs in the database, as one indexed EXISTS over the pins. Doing it in
    # Python instead meant loading every point the profile had ever pinned on
    # every wiki request, which grows without bound as a user's map fills up.
    # ``ST_Within(point, polygon)`` is the same OGC predicate as GEOS's
    # ``polygon.contains(point)``, just evaluated the other way round.
    containment = Q()
    for polygon in polygons:
        containment |= Q(location__point__within=polygon)
    return pins.exclude(location=location).filter(containment).exists()


def wikis_hidden_by_pin_move(pin: Pin, latitude: float, longitude: float) -> list[Wiki]:
    """Wikis the owner can see now but would lose by moving *pin* to this point.

    Only wikis this pin is actually keeping visible are returned: one the owner
    also reaches through another of their own pins is never listed, and neither
    is one they can't see in the first place. That makes an empty result the
    normal case, so callers can treat a non-empty one as genuinely worth
    interrupting the user for.

    Advisory only - it previews the rule rather than enforcing it. Access itself
    is always decided by :func:`location_visible_to` at read time, so a stale or
    slightly-off preview can never grant access, only mis-warn about it.

    Args:
        pin: The pin about to move.
        latitude: Proposed new latitude.
        longitude: Proposed new longitude.

    Returns:
        The affected wikis, each with its ``location`` selected. Empty when the
        move costs the owner nothing.
    """
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    if pin.location_id is None or pin.location.point is None:
        return []

    new_point = Point(float(longitude), float(latitude), srid=4326)

    # Everything this pin currently reaches: the wiki at its own Location, plus
    # any whose official boundary contains its point. Scoped to Locations that
    # have a wiki so this stays proportional to how many wikis exist rather than
    # to every boundary in the database.
    reachable_ids = {pin.location_id} | set(
        Boundary.objects.filter(
            pin__isnull=True,
            wiki__isnull=True,
            profile__isnull=True,
            source="",
            location__wiki__isnull=False,
            generated_polygon__contains=pin.location.point,
        ).values_list("location_id", flat=True),
    )
    candidates = list(Wiki.objects.filter(location_id__in=reachable_ids).select_related("location"))
    if not candidates:
        return []

    profile = pin.profile
    remaining = Pin.objects.filter(profile=profile).exclude(pk=pin.pk)
    return [wiki for wiki in candidates if location_visible_to(wiki.location, profile) and not _visible_given_pins(wiki.location, remaining, extra_point=new_point)]


def location_visible_to(location: Location, profile: Profile) -> bool:
    """Return ``True`` if *profile* has a pin at *location*, or inside its own boundary.

    A pin at the exact same ``Location`` row always qualifies. It also
    qualifies when the profile's pin is at a *different* Location row whose
    point falls inside *this* location's own generated boundary polygon -
    nearly-identical coordinates can resolve to distinct Location rows (the
    50 m ``get_nearby_or_create`` threshold vs. a larger building/property
    boundary), yet still be the same real-world place by every other measure
    the app already uses (e.g. the multi-location ambiguity check in
    ``post_add_pin``). Only ``generated_polygon`` (API-derived) on
    location-default ``Boundary`` rows is consulted - never a user-editable
    one - matching ``LocationQuerySet._boundary_polygon_q`` exactly, so this
    can't be gamed by inflating a boundary to see wikis for unrelated places.

    Args:
        location: The Location to check.
        profile: The viewing profile.

    Returns:
        Whether the profile has pinned this location, or another Location
        whose point falls inside this location's own boundary.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return _visible_given_pins(location, Pin.objects.filter(profile=profile))


def visible_wiki_location_ids(profile: Profile) -> set[int]:
    """Location ids of every Wiki visible to *profile* - the queryset-shaped
    counterpart to :func:`location_visible_to`, for callers that need the
    whole visible set at once rather than a single yes/no check (e.g. the
    custom-field REFERENCE picker's wiki queryset, ``custom_field_references
    .referenceable_queryset``).

    Includes every Location the profile has pinned directly, plus every
    OTHER Location whose own boundary polygon contains one of the profile's
    pinned points (see ``location_visible_to``'s docstring for why that
    second case exists - nearly-identical coordinates can resolve to
    distinct Location rows that are still the same real-world place).

    Scoped to only consider boundaries for Locations that actually have a
    Wiki - a naive version checking every location-default Boundary row in
    the database against every point the profile has ever pinned would be an
    unbounded N*M scan as the site grows; restricting the boundary side to
    "locations with a wiki" keeps it proportional to how many wikis exist,
    which is the only thing this check is ever used to find.

    Args:
        profile: The viewing profile.

    Returns:
        Set of Location primary keys whose Wiki is visible to *profile*.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.pin.model import Pin

    direct_ids = set(Pin.objects.filter(profile=profile).values_list("location_id", flat=True))

    # Same "location-default" boundary restriction as location_visible_to:
    # only generated_polygon on a Boundary with no owning pin/wiki/profile is
    # ever consulted here - never a user-editable one.
    boundary_candidates = list(
        Boundary.objects.filter(location__wiki__isnull=False, pin__isnull=True, wiki__isnull=True, profile__isnull=True, source="")
        .exclude(generated_polygon__isnull=True)
        .exclude(location_id__in=direct_ids)
        .values_list("location_id", "generated_polygon"),
    )
    if not boundary_candidates:
        return direct_ids

    points = []
    seen_location_ids: set[int] = set()
    for candidate_id, point in Pin.objects.filter(profile=profile).values_list("location_id", "location__point"):
        if point is None or candidate_id in seen_location_ids:
            continue
        seen_location_ids.add(candidate_id)
        points.append(point)

    matched_ids = {location_id for location_id, polygon in boundary_candidates if any(polygon.contains(point) for point in points)}
    return direct_ids | matched_ids


def resolve_visible_wiki(request: HttpRequest, location_slug: str) -> tuple[Location, Wiki, Profile]:
    """Resolve a Location and its Wiki, 404ing unless the requester has pinned that Location.

    A location with no wiki yet, a location whose only wiki is still an
    unofficial background-created draft (see ``Wiki.officially_created``), a
    location_slug that doesn't exist at all, and a real wiki the requester
    hasn't pinned all raise the identical ``Http404`` - deliberately
    indistinguishable, so guessing slugs can never reveal which locations
    other users have pinned (or which have a draft quietly being enriched).

    Wiki.location stays a strict one-to-one to a single canonical Location -
    ``location_visible_to`` is what actually absorbs the "nearly-identical
    coordinates, distinct Location rows" case, by accepting a pin at any
    boundary-mate Location too (see its docstring). Whether a Wiki should
    ever be reachable from more than one *canonical* Location slug (as
    opposed to just being visible to more pinners) is a separate, larger
    data-model question - not addressed here.

    Args:
        request: The current request (used for the requesting profile).
        location_slug: Slug of the Location whose Wiki is being resolved.

    Returns:
        Tuple of (Location, Wiki, requester's Profile).

    Raises:
        Http404: The location doesn't exist, has no wiki, or the requester
            has no pin there.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

    location = get_object_or_404(Location.objects.slug_or_uuid(location_slug))
    wiki = get_object_or_404(Wiki, location=location, officially_created=True)
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not location_visible_to(location, profile):
        raise Http404
    return location, wiki, profile
