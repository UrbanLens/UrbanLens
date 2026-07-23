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

from django.http import Http404
from django.shortcuts import get_object_or_404

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


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
    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.pin.model import Pin

    if Pin.objects.filter(profile=profile, location=location).exists():
        return True

    polygons = list(
        Boundary.objects.filter(location=location, pin__isnull=True, wiki__isnull=True, profile__isnull=True).exclude(generated_polygon__isnull=True).values_list("generated_polygon", flat=True),
    )
    if not polygons:
        return False

    # Python-side containment (GEOS .contains(), matching
    # LocationQuerySet._boundary_polygon_q's proven direction) rather than a
    # DB-side `within` lookup - `generated_polygon`/`point` are geography=True
    # fields, and PostGIS's containment functions are geometry-native, so
    # comparing them in Python avoids depending on an untested cross-type
    # operator. Candidate sets here are small: a handful of location-default
    # boundary rows per wiki, and one point per distinct pin location.
    seen_location_ids: set[int] = set()
    for candidate_id, point in Pin.objects.filter(profile=profile).exclude(location=location).values_list("location_id", "location__point"):
        if point is None or candidate_id in seen_location_ids:
            continue
        seen_location_ids.add(candidate_id)
        if any(polygon.contains(point) for polygon in polygons):
            return True
    return False


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
        Boundary.objects.filter(location__wiki__isnull=False, pin__isnull=True, wiki__isnull=True, profile__isnull=True).exclude(generated_polygon__isnull=True).exclude(location_id__in=direct_ids).values_list("location_id", "generated_polygon"),
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

    A location with no wiki yet, a location_slug that doesn't exist at all,
    and a real wiki the requester hasn't pinned all raise the identical
    ``Http404`` - deliberately indistinguishable, so guessing slugs can never
    reveal which locations other users have pinned.

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
    wiki = get_object_or_404(Wiki, location=location)
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not location_visible_to(location, profile):
        raise Http404
    return location, wiki, profile
