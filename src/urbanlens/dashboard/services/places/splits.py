"""Parcels that get subdivided, and the access that has to survive it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import GeometryCollection
from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation, PlaceStatus, is_plausible_area
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.places.lineage_events import LineageEvent, LineageEventKind, handle_lineage_event

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import GEOSGeometry, MultiPolygon

logger = logging.getLogger(__name__)

#: How much of a parcel's area has to disappear before a refresh is treated as a subdivision rather
#: than a boundary correction.
#: Providers routinely nudge outlines by a few percent; losing a third of a parcel is a different
#: kind of event.
SPLIT_SHRINK_RATIO = 0.66


#: A shrink that keeps more than this is a boundary nudge, whatever else is nearby.
SPLIT_KEEP_CEILING = 0.9

#: How much of the area that went missing has to be covered by another current parcel before a mild
#: shrink counts as a subdivision.
SPLIT_REMAINDER_COVERAGE = 0.5


def shrink_ratio(place: Place, new_geometry: MultiPolygon | None) -> float | None:
    """
    How much of a parcel's area the refreshed outline keeps.

    Returns:
        New area over old, or None when the comparison is not meaningful (not a parcel, no geometry,
        or an implausibly large parcel: that shrink is a correction, and a split would grandfather
        its holders into every successor).
    """
    if place.kind != PlaceKind.PARCEL or place.geometry is None or new_geometry is None or not is_plausible_area(place.kind, place.area_sqm):
        return None
    try:
        previous, current = place.geometry.area, new_geometry.area
    except (AttributeError, TypeError):
        return None
    if previous <= 0:
        return None
    return current / previous


def is_mild_shrink(place: Place, new_geometry: MultiPolygon | None) -> bool:
    """Whether the outline kept between the heavy-shrink threshold and :data:`SPLIT_KEEP_CEILING` of its area."""
    ratio = shrink_ratio(place, new_geometry)
    return ratio is not None and (1 - SPLIT_SHRINK_RATIO) <= ratio < SPLIT_KEEP_CEILING


def newer_parcels_over(place: Place, area: GEOSGeometry) -> list[MultiPolygon]:
    """Outlines of plausible current parcels, other than ``place``, that touch ``area`` and were created since ``place``'s geometry was recorded."""
    since = place.geometry_generated_at or place.created
    return [
        other.geometry
        for other in Place.objects.filter(kind=PlaceKind.PARCEL, status=PlaceStatus.CURRENT, geometry__intersects=area, created__gt=since).exclude(pk=place.pk)
        if other.geometry is not None and is_plausible_area(other.kind, other.area_sqm)
    ]


def remainder_is_covered(place: Place, new_geometry: MultiPolygon, seen: Iterable[MultiPolygon] = ()) -> bool:
    """
    Whether the part of the old outline the new one no longer holds is mostly another current parcel.

    Args:
        place: The parcel being refreshed.
        new_geometry: Its refreshed outline.
        seen: Parcel outlines returned in the same refresh.

    Returns:
        True when at least :data:`SPLIT_REMAINDER_COVERAGE` of the remainder is covered by ``seen`` or by
        a plausible current parcel created since ``place``'s geometry was recorded.
    """
    if place.geometry is None:
        return False
    remainder = place.geometry.difference(new_geometry)
    if remainder.empty or remainder.area <= 0:
        return False
    candidates = [*newer_parcels_over(place, remainder), *seen]
    if not candidates:
        return False
    covered = remainder.intersection(GeometryCollection(*candidates).unary_union)
    return (covered.area / remainder.area) >= SPLIT_REMAINDER_COVERAGE


def looks_like_a_split(place: Place, new_geometry: MultiPolygon | None, seen: Iterable[MultiPolygon] = ()) -> bool:
    """
    Whether refreshed geometry suggests this parcel was subdivided.

    Args:
        place: The parcel whose geometry is being refreshed.
        new_geometry: What the provider now returns for the same coordinate.
        seen: Other parcel outlines returned in the same refresh.

    Returns:
        True when the parcel has shrunk past :data:`SPLIT_SHRINK_RATIO`, or kept less than
        :data:`SPLIT_KEEP_CEILING` of its area with the remainder mostly covered by another current parcel.
        A pin falling outside the new outline is deliberately not a signal: it is what GPS noise at a parcel edge looks like.
    """
    ratio = shrink_ratio(place, new_geometry)
    if ratio is None or new_geometry is None:
        return False
    if ratio < (1 - SPLIT_SHRINK_RATIO):
        return True
    return ratio < SPLIT_KEEP_CEILING and remainder_is_covered(place, new_geometry, seen)


@transaction.atomic
def process_split(place: Place, successors: list[MultiPolygon]) -> Place:
    """
    Retire a parcel in favour of the parcels it was divided into.

    Args:
        place: The parcel being superseded.
        successors: Official outlines for the new parcels.

    Returns:
        The superseded place."""
    from urbanlens.dashboard.services.places.provisioning import upsert_place

    learned_at = timezone.now()

    children: list[Place] = []
    for geometry in successors:
        # exclude_pk=place.pk: place is still CURRENT here (status flips below, only once we know
        # this is really a split) - without it, a successor whose centroid falls near the original
        # parcel's own centroid (common: the original center often lands inside one of its own
        # pieces) would silently alias back onto place itself instead of becoming a new child.
        child = upsert_place(PlaceKind.PARCEL, geometry, name=place.name, exclude_pk=place.pk)
        if child is not None and child.pk != place.pk:
            lineage.set_parent(child, place, PlaceRelation.MEMBER_OF)
            children.append(child)

    if len(children) < 2:
        logger.info("process_split: place %s produced fewer than two successors; leaving it current", place.pk)
        return place

    Place.objects.filter(pk=place.pk).update(status=PlaceStatus.SUPERSEDED)
    place.status = PlaceStatus.SUPERSEDED

    # Buildings follow the ground they stand on, so each one re-homes to
    # whichever successor now contains it. One that fits none stays put; its
    # markers keep working through the superseded parent's domain.
    for building in Place.objects.filter(parent_id=place.pk, parent_relation=PlaceRelation.PART_OF, kind=PlaceKind.BUILDING):
        if building.geometry is None:
            continue
        new_home = next((child for child in children if child.geometry is not None and child.geometry.contains(building.geometry.centroid)), None)
        if new_home is not None:
            lineage.set_parent(building, new_home, PlaceRelation.PART_OF)

    # Snapshot before re-resolution moves anyone: after this, containment can
    # no longer prove what these profiles already had.
    handle_lineage_event(LineageEvent(LineageEventKind.SPLIT, [place], children, learned_at))

    for child in children:
        if child.geometry is not None:
            resolution.resolve_locations_in(child.geometry)
    lineage.refresh_derived_flags([place.pk, *[child.pk for child in children]])

    logger.info("process_split: place %s superseded by %s successors", place.pk, len(children))
    return place
