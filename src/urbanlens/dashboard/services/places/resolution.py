"""Resolving coordinates onto places, and keeping that cache honest.

``Location.place`` is a cache, not an identity. It answers "what is this
coordinate standing on?" and is recomputed whenever the answer could have
changed - a provider corrects a parcel outline, a building footprint arrives,
a split lands. Nothing keyed off ``Location`` (pin provenance, share exposure,
wiki routing) is disturbed when it moves, which is the whole reason place
membership is resolved rather than stored as an FK users' data depends on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models.functions import Area
from django.utils import timezone

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


def resolve_location_place(location: Location, *, save: bool = True) -> Place | None:
    """Resolve which place a Location's coordinate sits on.

    Never calls a provider - it only asks what is already known. Provisioning
    new geometry is :mod:`services.places.provisioning`'s job, so page renders
    and bulk re-resolution can call this freely.

    Args:
        location: The Location to resolve.
        save: Whether to persist the resolved FK and timestamp.

    Returns:
        The most specific current place containing the coordinate, or None
        when it is on no known parcel or building.
    """
    place = Place.objects.resolve_for_point(location.latitude, location.longitude)
    if save and location.place_id != (place.pk if place else None):
        # Deliberately not stamped when the answer is unchanged, and in
        # particular not when it is "no known place". ``place_resolved_at`` is
        # what ``services.locations.boundaries.generation_status`` reads as
        # "the provider chain has run here", and this function calls no
        # provider - so stamping an unresolved coordinate told the scheduler
        # that REData had already been asked about ground nothing had ever
        # looked at, and it was never asked again until the row went stale.
        # The chain stamps its own genuine miss in ``generate_location_boundaries``.
        stamped = timezone.now()
        Location.objects.filter(pk=location.pk).update(place=place, place_resolved_at=stamped)
        # Mirrored onto the instance because callers act on it immediately:
        # ``generate_location_boundaries`` reads this attribute right after
        # provisioning to decide whether to record a miss, and a stale None
        # there makes it clear the place that was just resolved.
        location.place_resolved_at = stamped
    location.place = place
    return place


def resolve_locations_in(polygon, *, exclude_place: Place | None = None) -> int:
    """Re-resolve every Location whose coordinate falls inside a polygon.

    Called after any change that could move locations between places: new or
    corrected geometry, a new building footprint carving itself out of its
    parcel, a split. Scoped by polygon so a boundary refresh never walks the
    whole table.

    Args:
        polygon: The area to re-resolve within; None is tolerated (no-op).
        exclude_place: Skip locations already resolved to this place, when the
            caller knows their answer cannot have changed.

    Returns:
        How many locations changed place.
    """
    if polygon is None:
        return 0
    candidates = Location.objects.filter(point__within=polygon)
    if exclude_place is not None:
        candidates = candidates.exclude(place=exclude_place)
    changed = 0
    for location in candidates.iterator(chunk_size=500):
        previous = location.place_id
        resolved = resolve_location_place(location)
        if previous != (resolved.pk if resolved else None):
            changed += 1
    if changed:
        logger.info("Re-resolved %s locations after a geometry change", changed)
    return changed


def refresh_area(place: Place) -> float | None:
    """Recompute and store a place's cached area in square metres.

    The area is what makes "most specific wins" an indexed sort rather than a
    PostGIS computation per candidate per request, so it has to be refreshed
    with the geometry it describes.

    Args:
        place: The place whose geometry has just changed.

    Returns:
        The area in square metres, or None when the place has no geometry.
    """
    if place.geometry is None:
        Place.objects.filter(pk=place.pk).update(area_sqm=None)
        place.area_sqm = None
        return None
    measured = Place.objects.filter(pk=place.pk).annotate(computed_area=Area("geometry")).values_list("computed_area", flat=True).first()
    area = float(measured.sq_m) if measured is not None else None
    Place.objects.filter(pk=place.pk).update(area_sqm=area)
    place.area_sqm = area
    return area


def attach_location(location: Location, place: Place | None) -> None:
    """Point a Location at a place without re-running containment.

    For callers that already know the answer - the bulk building import knows
    exactly which footprint it just created a pin for.

    Args:
        location: The Location to update.
        place: The resolved place, or None to clear.
    """
    Location.objects.filter(pk=location.pk).update(place=place, place_resolved_at=timezone.now())
    location.place = place
    location.place_resolved_at = timezone.now()


def domain_ids_for_locations(location_ids: Iterable[int]) -> set[int]:
    """The access domains a set of locations belong to.

    Args:
        location_ids: Location primary keys.

    Returns:
        Set of ``Place.domain_root_id`` values, skipping placeless locations.
    """
    ids = list(location_ids)
    if not ids:
        return set()
    return set(Location.objects.filter(pk__in=ids, place__isnull=False).values_list("place__domain_root_id", flat=True))
