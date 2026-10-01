"""Locations pinned by every member of a group of profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Exists, Min, OuterRef, Q

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def _on_a_shared_place() -> Q:
    """Pins whose place may be shared: they have one, and it is not implausibly large (``PlaceQuerySet.implausible``).

    Every other pin matches only its exact Location.
    """
    return Q(location__place__isnull=False) & ~Q(location__place_id__in=Place.objects.implausible().values("pk"))


def pins_sharing_a_place_with(profile: Profile) -> QuerySet[Pin]:
    """Anyone's pins on something *profile* has also pinned: the same place where a pin's location has one, else the same Location.

    Left in the database, so asking whether two people share a place reads one row however much either has pinned.

    Args:
        profile: The profile whose pinned places to match.

    Returns:
        A ``Pin`` queryset to narrow by owner, including *profile*'s own pins."""
    own = Pin.objects.filter(profile=profile, location__isnull=False)
    on_place = _on_a_shared_place()
    return Pin.objects.filter(
        Q(location__place_id__in=own.filter(on_place).values("location__place_id")) | Q(location_id__in=own.exclude(on_place).values("location_id")),
    )


def common_pin_location_ids(profiles: Sequence[Profile]) -> set[int]:
    """Return the ids of locations pinned by every one of ``profiles``.
    Two profiles count as sharing a place when their pins resolve onto the same real-world thing, not only when they land on the identical coordinate row (see :func:`pins_sharing_a_place_with`).

    Args:
        profiles: The profiles to intersect.

    Returns:
        The set of ``Location`` ids - one representative per shared place - pinned by all of ``profiles``."""
    if len(profiles) < 2:
        return set()
    on_place = _on_a_shared_place()
    shared = Pin.objects.filter(profile=profiles[0], location__isnull=False)
    for other in profiles[1:]:
        theirs = Pin.objects.filter(profile=other)
        shared = shared.filter(
            (Q(Exists(theirs.filter(location__place_id=OuterRef("location__place_id")))) & on_place) | (Q(Exists(theirs.filter(location_id=OuterRef("location_id")))) & ~on_place),
        )
    location_ids = set(shared.exclude(on_place).values_list("location_id", flat=True).distinct())
    # One representative Location per shared place, so a property pinned at several coordinates is listed once.
    location_ids.update(shared.filter(on_place).values("location__place_id").annotate(representative=Min("location_id")).values_list("representative", flat=True))
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
