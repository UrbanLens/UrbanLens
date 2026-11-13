"""Locations pinned by every member of a group of profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Min

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.same_place import on_a_shared_place, same_place_q

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def pins_sharing_a_place_with(profile: Profile) -> QuerySet[Pin]:
    """
    Anyone's pins on something *profile* has also pinned, by :func:`~urbanlens.dashboard.services.pins.same_place.same_place_q`.

    Left in the database, so asking whether two people share a place reads one row however much either has pinned.
    The result includes pins of strangers, so callers must only ever expose a yes/no or a count gated on a profile-to-profile permission, never the rows.

    Args:
        profile: The profile whose pinned places to match.

    Returns:
        A ``Pin`` queryset to narrow by owner, including *profile*'s own pins."""
    return Pin.objects.filter(same_place_q(Pin.objects.filter(profile=profile)))


def common_pin_location_ids(profiles: Sequence[Profile]) -> set[int]:
    """
    Return the ids of locations pinned by every one of ``profiles``.
    Two profiles count as sharing a place by :func:`~urbanlens.dashboard.services.pins.same_place.same_place`, not only when they land on the identical coordinate row.

    Every returned id is a Location of ``profiles[0]``'s own pins, so a caller passing the viewer first reveals nothing about anyone else's rows.

    Args:
        profiles: The profiles to intersect.

    Returns:
        The set of ``Location`` ids - one representative per shared place - pinned by all of ``profiles``."""
    if len(profiles) < 2:
        return set()
    on_place = on_a_shared_place()
    shared = Pin.objects.filter(profile=profiles[0], location__isnull=False)
    for other in profiles[1:]:
        shared = shared.filter(same_place_q(Pin.objects.filter(profile=other)))
    location_ids = set(shared.exclude(on_place).values_list("location_id", flat=True).distinct())
    # One representative Location per shared place, so a property pinned at several coordinates is listed once.
    location_ids.update(shared.filter(on_place).values("location__place_id").annotate(representative=Min("location_id")).values_list("representative", flat=True))
    return location_ids


def common_pin_locations(profiles: Sequence[Profile]) -> QuerySet[Location]:
    """
    Return the locations pinned by every one of ``profiles``.

    Args:
        profiles: The profiles to intersect.

    Returns:
        A ``Location`` queryset for the shared locations, or ``Location.objects.none()`` when there are none (or fewer than two profiles were given)."""
    common_ids = common_pin_location_ids(profiles)
    if not common_ids:
        return Location.objects.none()
    return Location.objects.filter(id__in=common_ids)
