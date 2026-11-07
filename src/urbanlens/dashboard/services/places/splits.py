"""Parcels that get subdivided, and the access that has to survive it."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import GeometryCollection, GEOSException
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation, PlaceStatus, is_plausible_area
from urbanlens.dashboard.services.places import geometry_versions, lineage, resolution
from urbanlens.dashboard.services.places.lineage_events import LineageEvent, LineageEventKind, handle_lineage_event

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import MultiPolygon

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


#: How many boundary refresh cycles (``SiteSettings.boundary_cache_days``) back an older outline still counts as the parcel's earlier extent.
SPLIT_VERSION_REFRESH_CYCLES = 2

#: Most recent outlines of one parcel considered.
MAX_VERSIONS_COMPARED = 20

#: Version provider for outlines chosen by a community boundary vote; never evidence of a provider's subdivision.
VOTE_PROVIDER = "vote"

#: Version provider of outlines from the provider chain, which records them without naming a provider.
CHAIN_PROVIDER = ""


def _pinned_by_outsiders(place: Place, holders: set[int]) -> bool:
    """Whether ``place``'s domain holds a pin from anyone outside ``holders``; an unrooted place counts, since its domain cannot be read."""
    from urbanlens.dashboard.models.pin.model import Pin

    if place.domain_root_id is None:
        return True
    return Pin.objects.filter(location__place__domain_root_id=place.domain_root_id).exclude(profile_id__in=holders).exists()


def candidate_parcels(place: Place, new_geometry: MultiPolygon, *, source: str = CHAIN_PROVIDER) -> list[MultiPolygon]:
    """
    Outlines of stored parcels that could be the rest of ``place``.

    A candidate is a plausible CURRENT root parcel whose outline ``source`` (the provider of ``new_geometry``) drew, created since ``place``'s
    geometry was recorded, essentially disjoint from the new outline. One whose domain holds pins
    from anyone who did not hold ``place`` is skipped and logged: re-parenting it would hand strangers access.
    A remainder parcel that only appears on a later refresh is not stored yet when the shrink happens;
    :func:`detect_split_from_versions` finds that case from the older outline.
    """
    from urbanlens.dashboard.services.places.lineage_events import holders_of

    if place.geometry is None:
        return []
    try:
        remainder = place.geometry.difference(new_geometry)
    except GEOSException:
        logger.warning("split candidates of %s skipped: invalid provider outline", place.pk)
        return []
    if remainder.empty:
        return []
    since = place.geometry_generated_at or place.created
    queryset = Place.objects.filter(
        kind=PlaceKind.PARCEL,
        status=PlaceStatus.CURRENT,
        geometry__intersects=remainder,
        created__gt=since,
        parent__isnull=True,
    ).exclude(pk=place.pk)
    holders: set[int] | None = None
    geometries: list[MultiPolygon] = []
    for other in queryset:
        if other.geometry is None or not is_plausible_area(other.kind, other.area_sqm) or other.geometry.area <= 0:
            continue
        if geometry_versions.outline_source(other) != source:
            continue
        try:
            if other.geometry.intersection(new_geometry).area / other.geometry.area > SPLIT_OVERLAP_TOLERANCE:
                continue
        except GEOSException:
            logger.warning("split candidate %s skipped: invalid outline", other.pk)
            continue
        if holders is None:
            holders = holders_of([place], timezone.now())
        if _pinned_by_outsiders(other, holders):
            logger.info("split candidate %s skipped: its domain has pins from outside place %s", other.pk, place.pk)
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
    if place.geometry is None:
        return False
    try:
        remainder = place.geometry.difference(new_geometry)
        if remainder.empty or remainder.area <= 0:
            return False
        candidates = [*candidate_parcels(place, new_geometry), *seen]
        if not candidates:
            return False
        union = GeometryCollection(*candidates).unary_union
        if (remainder.intersection(union).area / remainder.area) < SPLIT_REMAINDER_COVERAGE:
            return False
        rebuilt = place.geometry.intersection(new_geometry.union(union))
        return (rebuilt.area / place.geometry.area) >= SPLIT_REBUILD_RATIO
    except GEOSException:
        logger.warning("split check of %s skipped: invalid provider outline", place.pk)
        return False


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


def detect_split_from_versions(new_parcel: Place) -> Place | None:
    """
    Notice a split that completes only when the remainder parcel arrives.

    A parcel that shrank by 10-66% was left alone at refresh time, because nothing yet covered what it lost.
    When a new parcel is created, look at the older outlines of nearby root parcels (and pieces of earlier splits) that the new parcel's provider drew: if the
    new parcel fills the gap between an old outline and the current one, that parcel was split.

    Args:
        new_parcel: A root parcel just created.

    Returns:
        The superseded place, or None when no split was found.
    """
    new_geometry = new_parcel.geometry
    if new_geometry is None or new_geometry.area <= 0 or new_parcel.kind != PlaceKind.PARCEL:
        return None
    # A piece of an earlier split is a parcel in its own right and can be divided again.
    standalone = Q(parent__isnull=True) | Q(parent_relation=PlaceRelation.MEMBER_OF, parent__status=PlaceStatus.SUPERSEDED)
    # Older outlines count too: the remainder can sit in a gap from the parcel's current outline.
    candidates = Place.objects.filter(standalone, kind=PlaceKind.PARCEL, status=PlaceStatus.CURRENT).filter(Q(geometry__intersects=new_geometry) | Q(geometry_versions__geometry__intersects=new_geometry)).exclude(pk=new_parcel.pk).distinct()
    # Compared by who drew the outlines: Place.provider is a key namespace, and a place keeps it whoever refreshes the outline.
    source = geometry_versions.outline_source(new_parcel)
    candidates = candidates.filter(geometry_versions__provider=source)
    from urbanlens.dashboard.models.site_settings import SiteSettings

    horizon = timezone.now() - timedelta(days=SPLIT_VERSION_REFRESH_CYCLES * SiteSettings.get_current().boundary_cache_days)
    for previous in candidates:
        current = previous.geometry
        if current is None or current.area <= 0 or not is_plausible_area(previous.kind, previous.area_sqm):
            continue
        versions = list(previous.geometry_versions.filter(erroneous=False).order_by("-recorded_at", "-pk")[: 1 + MAX_VERSIONS_COMPARED])
        # A vote moves the outline without the provider saying the parcel changed, so only the provider's own earlier outlines count.
        if not versions or versions[0].provider == VOTE_PROVIDER or versions[0].provider != source:
            continue
        older = [version.geometry for version in versions[1:] if version.provider == source and version.recorded_at >= horizon]
        before = max(older, key=lambda geometry: geometry.area, default=None)
        if before is None or before.area <= 0 or current.area / before.area >= SPLIT_KEEP_CEILING:
            continue
        try:
            # The new parcel must sit in the space the old one gave up, not overlap what it kept.
            if new_geometry.intersection(current).area / new_geometry.area > SPLIT_OVERLAP_TOLERANCE:
                continue
            remainder = before.difference(current)
            if remainder.empty or remainder.area <= 0 or remainder.intersection(new_geometry).area / remainder.area < SPLIT_REMAINDER_COVERAGE:
                continue
            if before.intersection(current.union(new_geometry)).area / before.area < SPLIT_REBUILD_RATIO:
                continue
        except GEOSException:
            logger.warning("version split of %s skipped: invalid provider outline", previous.pk)
            continue
        # process_split leaves out a new parcel whose domain has pins from outside previous, which leaves no split.
        if process_split(previous, [current, new_geometry], source=source).status == PlaceStatus.SUPERSEDED:
            return previous
    return None


@transaction.atomic
def process_split(place: Place, successors: list[MultiPolygon], *, source: str = CHAIN_PROVIDER) -> Place:
    """
    Retire a parcel in favour of the parcels it was divided into.

    Args:
        place: The parcel being superseded.
        successors: Official outlines for the new parcels.
        source: The provider that drew ``successors``, recorded on their outlines so later version splits find them.

    Returns:
        The superseded place."""
    from urbanlens.dashboard.services.places.lineage_events import holders_of
    from urbanlens.dashboard.services.places.provisioning import find_matching_place, upsert_place

    learned_at = timezone.now()
    holders = holders_of([place], learned_at)

    children: list[Place] = []
    previous_parents: dict[int, tuple[Place | None, str]] = {}
    for geometry in successors:
        # place is still CURRENT, so without exclude_pk a piece holding its centroid would alias back onto it.
        existing = find_matching_place(PlaceKind.PARCEL, geometry, exclude_pk=place.pk)
        if existing is not None and _pinned_by_outsiders(existing, holders):
            # Re-parenting it would grant place's holders a domain other people pinned in.
            logger.info("split successor skipped: it matches parcel %s, whose domain has pins from outside place %s", existing.pk, place.pk)
            continue
        child = upsert_place(PlaceKind.PARCEL, geometry, provider=source, name=place.name, exclude_pk=place.pk, detect_splits=False)
        if child is not None and child.pk != place.pk:
            previous_parents[child.pk] = (child.parent, child.parent_relation)
            lineage.set_parent(child, place, PlaceRelation.MEMBER_OF)
            children.append(child)

    if len(children) < 2:
        logger.info("process_split: place %s produced fewer than two successors; leaving it current", place.pk)
        for child in children:
            old_parent, old_relation = previous_parents[child.pk]
            if old_parent is None:
                lineage.set_parent(child, None)
            else:
                lineage.set_parent(child, old_parent, old_relation)
        return place

    Place.objects.filter(pk=place.pk).update(status=PlaceStatus.SUPERSEDED)
    place.status = PlaceStatus.SUPERSEDED

    # Before buildings re-home: afterwards their pins no longer prove what the holders had.
    handle_lineage_event(LineageEvent(LineageEventKind.SPLIT, [place], children, learned_at))

    # A building fitting no successor stays put and keeps working through the superseded domain.
    for building in Place.objects.filter(parent_id=place.pk, parent_relation=PlaceRelation.PART_OF, kind=PlaceKind.BUILDING):
        if building.geometry is None:
            continue
        try:
            new_home = next((child for child in children if child.geometry is not None and child.geometry.contains(building.geometry.centroid)), None)
        except GEOSException:
            logger.warning("process_split: building %s stays on %s: invalid outline", building.pk, place.pk)
            continue
        if new_home is not None:
            lineage.set_parent(building, new_home, PlaceRelation.PART_OF)

    for child in children:
        if child.geometry is not None:
            resolution.resolve_locations_in(child.geometry)
    lineage.refresh_derived_flags([place.pk, *[child.pk for child in children]])

    logger.info("process_split: place %s superseded by %s successors", place.pk, len(children))
    return place
