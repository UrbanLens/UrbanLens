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
        Boundary.objects.filter(location=location, pin__isnull=True, wiki__isnull=True, profile__isnull=True)
        .exclude(generated_polygon__isnull=True)
        .values_list("generated_polygon", flat=True),
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
