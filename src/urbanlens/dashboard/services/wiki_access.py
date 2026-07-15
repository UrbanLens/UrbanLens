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
    """Return ``True`` if *profile* has a pin at *location*.

    Args:
        location: The Location to check.
        profile: The viewing profile.

    Returns:
        Whether the profile has pinned this location (the only thing that
        makes its wiki visible to them).
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.filter(profile=profile, location=location).exists()


def resolve_visible_wiki(request: HttpRequest, location_slug: str) -> tuple[Location, Wiki, Profile]:
    """Resolve a Location and its Wiki, 404ing unless the requester has pinned that Location.

    A location with no wiki yet, a location_slug that doesn't exist at all,
    and a real wiki the requester hasn't pinned all raise the identical
    ``Http404`` - deliberately indistinguishable, so guessing slugs can never
    reveal which locations other users have pinned.

    TODO: We need the wiki->location relationship to be a FK, not a O2O... because a wiki applies
    to any coordinates in a boundary, and multiple locations will have coordinates in that boundary.
    This is slightly tricky, because we currently attach boundaries to Locations, which means two
    coordinates that are nearly identical could theoretically have substantially different boundaries,
    resulting in slightly unexpected behavior when matching wikis to coordinates.

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
