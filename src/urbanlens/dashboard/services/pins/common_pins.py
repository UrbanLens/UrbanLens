"""Locations pinned by every member of a group of profiles.

Used today for the pairwise "Places in Common" stat/page on the profile
page, but written to intersect any number of profiles so it also covers
the "expand to groups, e.g. trips" follow-up called out in the same
feature request without a later rewrite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def _pinned_keys(profile: Profile) -> set[tuple[str, int]]:
    """One key per real-world thing this profile has pinned.

    Keyed by *place* where one is known, falling back to the exact Location
    otherwise. That is what makes the count mean what people expect it to:
    two friends who explored the same property and pinned it fifty metres
    apart used to show zero places in common, because their coordinates
    resolved to different Location rows.

    Args:
        profile: The profile whose pins to key.

    Returns:
        Set of ``("place", id)`` / ``("location", id)`` keys.
    """
    keys: set[tuple[str, int]] = set()
    for location_id, place_id in Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", "location__place_id"):
        keys.add(("place", place_id) if place_id is not None else ("location", location_id))
    return keys


def common_pin_location_ids(profiles: Sequence[Profile]) -> set[int]:
    """Return the ids of locations pinned by every one of ``profiles``.

    Two profiles count as sharing a place when their pins resolve onto the
    same real-world thing, not only when they land on the identical
    coordinate row (see :func:`_pinned_keys`).

    Args:
        profiles: The profiles to intersect. Fewer than two profiles can
            never have anything "in common", so that case always returns
            an empty set rather than one profile's full pin list.

    Returns:
        The set of ``Location`` ids - one representative per shared place -
        pinned by all of ``profiles``.
    """
    if len(profiles) < 2:
        return set()
    shared = set.intersection(*[_pinned_keys(profile) for profile in profiles])
    if not shared:
        return set()

    place_ids = [pk for kind, pk in shared if kind == "place"]
    location_ids = {pk for kind, pk in shared if kind == "location"}
    if place_ids:
        # One representative Location per shared place: the callers render a
        # list of places, and listing the same property once per coordinate
        # anybody pinned it at would be the old duplication all over again.
        for place_id in place_ids:
            representative = Pin.objects.filter(profile=profiles[0], location__place_id=place_id).values_list("location_id", flat=True).first()
            if representative is not None:
                location_ids.add(representative)
    return location_ids


def common_pin_locations(profiles: Sequence[Profile]) -> QuerySet[Location]:
    """Return the locations pinned by every one of ``profiles``.

    Args:
        profiles: The profiles to intersect.

    Returns:
        A ``Location`` queryset for the shared locations, or ``Location.objects.none()``
        when there are none (or fewer than two profiles were given).
    """
    common_ids = common_pin_location_ids(profiles)
    if not common_ids:
        return Location.objects.none()
    return Location.objects.filter(id__in=common_ids)
