"""Parcels that get subdivided, and the access that has to survive it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import GeometryCollection
from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation, PlaceStatus, is_plausible_area
from urbanlens.dashboard.services.geo.area import area_sqm
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


#: Most of a covering parcel's area that may lie inside the new outline. A neighbour that overlaps it is a different parcel, not the rest of this one.
SPLIT_OVERLAP_TOLERANCE = 0.05

#: How much of the old outline the new outline plus the covering parcels must rebuild.
SPLIT_REBUILD_RATIO = 0.8


#: Least share of a successor's own area that must lie in the part of the old outline the new one gave up.
#: A parcel that only touches or clips the old outline is a neighbour, not a piece of it.
SPLIT_REMAINDER_SHARE = 0.5


def remainder_of(place: Place, new_geometry: MultiPolygon) -> GEOSGeometry | None:
    """
    The part of ``place``'s outline that ``new_geometry`` no longer holds.

    Returns:
        The remainder, or None when there is none.
    """
    if place.geometry is None:
        return None
    remainder = place.geometry.difference(new_geometry)
    if remainder.empty or remainder.area <= 0:
        return None
    return remainder


def is_remainder_piece(geometry: MultiPolygon | None, new_geometry: MultiPolygon, remainder: GEOSGeometry) -> bool:
    """
    Whether an outline could be one of the parcels the rest of the old one became.

    The rule every successor answers to, stored or freshly probed: a plausible parcel, essentially disjoint from
    the new outline (:data:`SPLIT_OVERLAP_TOLERANCE`), lying mostly in the remainder (:data:`SPLIT_REMAINDER_SHARE`).

    Args:
        geometry: The candidate outline.
        new_geometry: The refreshed outline of the parcel being split.
        remainder: :func:`remainder_of` that parcel.
    """
    if geometry is None or geometry.area <= 0 or not is_plausible_area(PlaceKind.PARCEL, area_sqm(geometry)):
        return False
    if geometry.intersection(new_geometry).area / geometry.area > SPLIT_OVERLAP_TOLERANCE:
        return False
    return geometry.intersection(remainder).area / geometry.area >= SPLIT_REMAINDER_SHARE


def has_stranger_pins(place: Place, other: Place, holders: set[int]) -> bool:
    """
    Whether ``other``'s domain holds pins from anyone outside ``holders``; re-parenting it would hand them access.

    Args:
        place: The parcel being split, for the log line.
        other: The parcel that would become its successor.
        holders: :func:`~urbanlens.dashboard.services.places.lineage_events.holders_of` ``place``.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    if other.domain_root_id is None:
        strangers = Pin.objects.filter(location__place_id=other.pk)
    else:
        strangers = Pin.objects.filter(location__place__domain_root_id=other.domain_root_id)
    if strangers.exclude(profile_id__in=holders).exists():
        logger.info("split successor %s skipped: its domain has pins from outside place %s", other.pk, place.pk)
        return True
    return False


def candidate_parcels(place: Place, new_geometry: MultiPolygon) -> list[MultiPolygon]:
    """
    Outlines of stored parcels that could be the rest of ``place``.

    A candidate is a CURRENT root parcel from the same provider, created since ``place``'s geometry was recorded,
    that passes :func:`is_remainder_piece`. One whose domain holds pins from anyone who did not hold ``place`` is
    skipped and logged (:func:`has_stranger_pins`).
    The geometry is overwritten in place, so a remainder parcel that only appears on a later refresh is never
    seen here; append-only geometry versions (UL-362) would close that.
    """
    from urbanlens.dashboard.services.places.lineage_events import holders_of

    remainder = remainder_of(place, new_geometry)
    if remainder is None:
        return []
    since = place.geometry_generated_at or place.created
    queryset = Place.objects.filter(
        kind=PlaceKind.PARCEL,
        status=PlaceStatus.CURRENT,
        geometry__intersects=remainder,
        created__gt=since,
        provider=place.provider,
        parent__isnull=True,
    ).exclude(pk=place.pk)
    holders: set[int] | None = None
    geometries: list[MultiPolygon] = []
    for other in queryset:
        if other.geometry is None or not is_remainder_piece(other.geometry, new_geometry, remainder):
            continue
        if holders is None:
            holders = holders_of([place], timezone.now())
        if has_stranger_pins(place, other, holders):
            continue
        geometries.append(other.geometry)
    return geometries


def remainder_is_covered(place: Place, new_geometry: MultiPolygon, seen: Iterable[MultiPolygon] = ()) -> bool:
    """
    Whether the part of the old outline the new one no longer holds is another current parcel.

    Args:
        place: The parcel being refreshed.
        new_geometry: Its refreshed outline.
        seen: Parcel outlines returned in the same refresh.

    Returns:
        True when :func:`candidate_parcels` and ``seen`` cover at least :data:`SPLIT_REMAINDER_COVERAGE` of the
        remainder and, with the new outline, rebuild at least :data:`SPLIT_REBUILD_RATIO` of the old one.
    """
    remainder = remainder_of(place, new_geometry)
    if place.geometry is None or remainder is None:
        return False
    candidates = [*candidate_parcels(place, new_geometry), *seen]
    if not candidates:
        return False
    union = GeometryCollection(*candidates).unary_union
    if (remainder.intersection(union).area / remainder.area) < SPLIT_REMAINDER_COVERAGE:
        return False
    rebuilt = place.geometry.intersection(new_geometry.union(union))
    return (rebuilt.area / place.geometry.area) >= SPLIT_REBUILD_RATIO


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

    # Snapshot before anything moves: once buildings re-home, containment can
    # no longer prove what these profiles already had.
    handle_lineage_event(LineageEvent(LineageEventKind.SPLIT, [place], children, learned_at))

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

    for child in children:
        if child.geometry is not None:
            resolution.resolve_locations_in(child.geometry)
    lineage.refresh_derived_flags([place.pk, *[child.pk for child in children]])

    logger.info("process_split: place %s superseded by %s successors", place.pk, len(children))
    return place
