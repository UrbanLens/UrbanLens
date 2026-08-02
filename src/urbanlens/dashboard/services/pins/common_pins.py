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


def common_pin_location_ids(profiles: Sequence[Profile]) -> set[int]:
    """Return the ids of locations pinned by every one of ``profiles``.

    Args:
        profiles: The profiles to intersect. Fewer than two profiles can
            never have anything "in common", so that case always returns
            an empty set rather than one profile's full pin list.

    Returns:
        The set of ``Location`` ids pinned by all of ``profiles``.
    """
    if len(profiles) < 2:
        return set()
    location_id_sets = [set(Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True)) for profile in profiles]
    return set.intersection(*location_id_sets)


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
