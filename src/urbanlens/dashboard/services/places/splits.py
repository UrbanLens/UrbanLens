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
from urbanlens.dashboard.services.places.lineage_events import LineageEvent, LineageEventKind, handle_lineage_event, holders_of

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


def is_piece_of(place: Place, geometry: MultiPolygon | None, pieces: Iterable[MultiPolygon]) -> bool:
    """
    Whether an outline can be one more of the parcels ``place`` was divided into, beside ``pieces``.

    The geometric rule :func:`process_split` holds every successor to, whichever path found it, so no caller can hand
    it a neighbour: a plausible parcel lying mostly (:data:`SPLIT_REMAINDER_SHARE`) inside the old outline and
    essentially disjoint (:data:`SPLIT_OVERLAP_TOLERANCE`) from every piece already taken. It needs no remainder, so
    it holds for the refreshed outline too; with that outline first it is what :func:`is_remainder_piece` asks upstream.

    Args:
        place: The parcel being split.
        geometry: The candidate outline.
        pieces: Outlines already accepted as successors.
    """
    if place.geometry is None or geometry is None or geometry.area <= 0 or not is_plausible_area(PlaceKind.PARCEL, area_sqm(geometry)):
        return False
    if geometry.intersection(place.geometry).area / geometry.area < SPLIT_REMAINDER_SHARE:
        return False
    return all(geometry.intersection(piece).area / geometry.area <= SPLIT_OVERLAP_TOLERANCE for piece in pieces)


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


def is_safe_successor(place: Place, other: Place, holders: set[int]) -> bool:
    """
    Whether a stored parcel may become a successor of ``place``.

    The rule every successor that resolves to a stored parcel answers to, wherever its outline came from (a stored
    candidate, a probe, or the refreshed outline itself): re-parenting must neither rewrite another family nor hand
    strangers access (:func:`has_stranger_pins`).

    Args:
        place: The parcel being split.
        other: The stored parcel the successor outline describes.
        holders: :func:`~urbanlens.dashboard.services.places.lineage_events.holders_of` ``place``.
    """
    if other.parent_id is not None and other.parent_id != place.pk:
        logger.info("split successor %s skipped: it already belongs to place %s", other.pk, other.parent_id)
        return False
    return not has_stranger_pins(place, other, holders)


def candidate_parcels(place: Place, new_geometry: MultiPolygon) -> list[MultiPolygon]:
    """
    Outlines of stored parcels that could be the rest of ``place``.

    A candidate is a CURRENT root parcel from the same provider, created since ``place``'s geometry was recorded,
    that passes :func:`is_remainder_piece` and :func:`is_safe_successor`.
    The geometry is overwritten in place, so a remainder parcel that only appears on a later refresh is never
    seen here; append-only geometry versions (UL-362) would close that.
    """
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
        if not is_safe_successor(place, other, holders):
            continue
        geometries.append(other.geometry)
    return geometries


def remainder_is_covered(place: Place, new_geometry: MultiPolygon, seen: Iterable[MultiPolygon] = ()) -> bool:
    """
    Whether the part of the old outline the new one no longer holds is another current parcel.

    Args:
        place: The parcel being refreshed.
        new_geometry: Its refreshed outline.
        seen: Parcel outlines returned in the same refresh. Only those passing :func:`is_remainder_piece` count, the
            rule stored candidates answer to, so an overlapping or outlying neighbour cannot make up the coverage.

    Returns:
        True when :func:`candidate_parcels` and ``seen`` cover at least :data:`SPLIT_REMAINDER_COVERAGE` of the
        remainder and, with the new outline, rebuild at least :data:`SPLIT_REBUILD_RATIO` of the old one.
    """
    remainder = remainder_of(place, new_geometry)
    if place.geometry is None or remainder is None:
        return False
    candidates = [*candidate_parcels(place, new_geometry), *(outline for outline in seen if is_remainder_piece(outline, new_geometry, remainder))]
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
def process_split(place: Place, successors: list[MultiPolygon]) -> Place | None:
    """
    Retire a parcel in favour of the parcels it was divided into.

    This is the one place every successor passes through, the refreshed outline included, so the successor rules
    are enforced here whichever path found the outline: one failing :func:`is_piece_of`, or describing a stored
    parcel that fails :func:`is_safe_successor`, is left out.

    Args:
        place: The parcel being superseded.
        successors: Official outlines for the new parcels, the refreshed outline first when there is one.

    Returns:
        The superseded place, or None when fewer than two successors survive and the parcel stays current."""
    from urbanlens.dashboard.services.places.provisioning import find_matching_place, upsert_place

    learned_at = timezone.now()

    # exclude_pk=place.pk: place is still CURRENT until the split commits, and its centroid often lands inside one
    # of its own pieces, which would otherwise alias back onto it. This is the lookup upsert_place makes, and all
    # are checked before any is touched, so a split that falls short re-parents nothing.
    accepted: list[MultiPolygon] = []
    holders: set[int] | None = None
    for geometry in successors:
        if not is_piece_of(place, geometry, accepted):
            logger.info("process_split: an outline offered for place %s is not a piece of it; left out", place.pk)
            continue
        matched = find_matching_place(PlaceKind.PARCEL, geometry, exclude_pk=place.pk)
        if matched is not None:
            if holders is None:
                holders = holders_of([place], learned_at)
            if not is_safe_successor(place, matched, holders):
                continue
        accepted.append(geometry)
    if len(accepted) < 2:
        logger.info("process_split: place %s has fewer than two safe successors; leaving it current", place.pk)
        return None

    children: list[Place] = []
    for geometry in accepted:
        child = upsert_place(PlaceKind.PARCEL, geometry, name=place.name, exclude_pk=place.pk)
        if child is not None and child.pk != place.pk:
            lineage.set_parent(child, place, PlaceRelation.MEMBER_OF)
            children.append(child)

    if len(children) < 2:
        logger.info("process_split: place %s produced fewer than two successors; leaving it current", place.pk)
        return None

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
