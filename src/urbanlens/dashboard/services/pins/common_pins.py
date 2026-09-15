"""Locations pinned by every member of a group of profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def pinned_place_keys(profile: Profile) -> set[tuple[str, int]]:
    """One key per real-world thing this profile has pinned.
    Keyed by *place* where one is known, falling back to the exact Location otherwise.

    Args:
        profile: The profile whose pins to key.

    Returns:
        Set of ``("place", id)`` / ``("location", id)`` keys."""
    keys: set[tuple[str, int]] = set()
    for location_id, place_id in Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", "location__place_id"):
        keys.add(("place", place_id) if place_id is not None else ("location", location_id))
    return keys


def pins_sharing_a_place_with(profile: Profile) -> QuerySet[Pin]:
    """Anyone's pins on something *profile* has also pinned, keyed as :func:`pinned_place_keys` keys it.

    Left in the database, so asking whether two people share a place reads one row however much either has pinned.

    Args:
        profile: The profile whose pinned places to match.

    Returns:
        A ``Pin`` queryset to narrow by owner, including *profile*'s own pins."""
    own = Pin.objects.filter(profile=profile, location__isnull=False)
    return Pin.objects.filter(
        Q(location__place_id__in=own.exclude(location__place__isnull=True).values("location__place_id")) | Q(location_id__in=own.filter(location__place__isnull=True).values("location_id")),
    )


def common_pin_location_ids(profiles: Sequence[Profile]) -> set[int]:
    """Return the ids of locations pinned by every one of ``profiles``.
    Two profiles count as sharing a place when their pins resolve onto the same real-world thing, not only when they land on the identical coordinate row (see :func:`pinned_place_keys`).

    Args:
        profiles: The profiles to intersect.

    Returns:
        The set of ``Location`` ids - one representative per shared place - pinned by all of ``profiles``."""
    if len(profiles) < 2:
        return set()
    shared = set.intersection(*[pinned_place_keys(profile) for profile in profiles])
    if not shared:
        return set()

    place_ids = [pk for kind, pk in shared if kind == "place"]
    location_ids = {pk for kind, pk in shared if kind == "location"}
    if place_ids:
        # One representative Location per shared place: the callers render a
        # list of places, and listing the same property once per coordinate
        # anybody pinned it at would be the old duplication all over again.
        # Picked in one pass rather than a query per place - the number of
        # places two accounts share is not something either of them chose.
        representatives: dict[int, int] = {}
        rows = Pin.objects.filter(profile=profiles[0], location__place_id__in=place_ids).order_by("location_id").values_list("location__place_id", "location_id")
        for place_id, location_id in rows:
            representatives.setdefault(place_id, location_id)
        location_ids.update(representatives.values())
    return location_ids


def common_pin_locations(profiles: Sequence[Profile]) -> QuerySet[Location]:
    """Return the locations pinned by every one of ``profiles``.

    Args:
        profiles: The profiles to intersect.

    Returns:
        A ``Location`` queryset for the shared locations, or ``Location.objects.none()`` when there are none (or fewer than two profiles were given)."""
    common_ids = common_pin_location_ids(profiles)
    if not common_ids:
        return Location.objects.none()
    return Location.objects.filter(id__in=common_ids)
