"""The one answer to "are these two pins the same place?"."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


def on_a_shared_place(prefix: str = "location__") -> Q:
    """
    Rows whose Location has a Place that is not implausibly large (``PlaceQuerySet.implausible``).

    Every other row matches only its exact Location.

    Args:
        prefix: Lookup path from the filtered model to its ``Location``; empty when filtering ``Location`` itself.
    """
    return Q(**{f"{prefix}place__isnull": False}) & ~Q(**{f"{prefix}place_id__in": Place.objects.implausible().values("pk")})


def _shares_a_place(location_a: Location, location_b: Location) -> bool:
    """
    The single rule behind :func:`same_place` and :func:`same_place_q`; widen it here.

    Two Locations are the same place when both resolve to one shared Place. Everything else compares the exact Location:

    - Placeless, or on an implausibly large Place: only the identical Location row.
    - Two buildings in one parcel are different places, and so are a parcel and a building inside it. ``Location.place`` is the most specific
      Place a point resolved to, so the parcel is never "the same" as one of its buildings.
    - Places linked by split lineage are different places (R-46 is open).
    """
    if location_a.pk == location_b.pk:
        return True
    if location_a.place_id is None or location_a.place_id != location_b.place_id:
        return False
    return not Place.objects.implausible().filter(pk=location_a.place_id).exists()


def same_place(pin_a: Pin, pin_b: Pin) -> bool:
    """
    Whether two pins are the same place.

    Args:
        pin_a: Any pin.
        pin_b: Any pin. The caller is responsible for being allowed to see both.

    Returns:
        True when both pins' Locations resolve to the same Place, or are the identical Location.
    """
    return _shares_a_place(pin_a.location, pin_b.location)


def same_place_q(pins: QuerySet[Pin], *, prefix: str = "location__") -> Q:
    """
    A ``Q`` matching rows at the same place as any of *pins*; the set-based :func:`same_place`.

    The comparison stays in the database, as two ``IN`` subqueries however many pins are involved.

    Args:
        pins: The pins to match against. Callers narrow these to pins the viewer may already see.
        prefix: Lookup path from the filtered model to its ``Location``; empty when filtering ``Location`` itself.

    Returns:
        A ``Q`` to filter ``Pin`` (or anything with a Location, such as ``Wiki``) with.
    """
    anchored = pins.filter(location__isnull=False)
    on_place = on_a_shared_place("location__")
    id_field = f"{prefix}pk" if prefix else "pk"
    return Q(**{f"{prefix}place_id__in": anchored.filter(on_place).values("location__place_id")}) | Q(**{f"{id_field}__in": anchored.values("location_id")})


def locations_sharing_a_place_with(profile: Profile, location_ids: Iterable[int]) -> set[int]:
    """
    Which of *location_ids* are the same place as one of *profile*'s pins; the batch form of :func:`same_place` for a list of Locations.

    Args:
        profile: Whose pins to match against.
        location_ids: Candidate Locations, e.g. those of activities on a trip.

    Returns:
        The subset at the same place as a pin of *profile*. One query.
    """
    from urbanlens.dashboard.models.location.model import Location

    ids = {pk for pk in location_ids if pk is not None}
    if not ids:
        return set()
    return set(Location.objects.filter(same_place_q(Pin.objects.filter(profile=profile), prefix=""), pk__in=ids).values_list("pk", flat=True))
