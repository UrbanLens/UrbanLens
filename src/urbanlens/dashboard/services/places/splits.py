"""Parcels that get subdivided, and the access that has to survive it.

A property sold off in pieces stops being one parcel and becomes several. The
old outline still geometrically contains every pin anyone ever dropped inside
it, so leaving it in play would let containment against historical geometry
grant access to a campus that no longer exists - hence
:class:`~urbanlens.dashboard.models.place.model.PlaceStatus`, and hence
``PlaceQuerySet.resolvable`` excluding superseded rows.

What makes automatic processing safe is the grant snapshot. Nobody can lose
access to a wiki they already hold, so a false positive costs a redundant row
and an unnecessary aggregate, never someone's access. That in turn is why the
detection threshold below can be generous.

The snapshot covers the whole family, not just the superseded parcel: a
holder of the undivided parcel knew everything that ground now contains, so
they are granted the parcel *and* every one of its new successors together
(:meth:`PlaceAccessGrantManager.snapshot_family`) - permanently, regardless of
which successor their own pin happens to re-resolve onto.

Deliberately *not* done here: creating child wikis. A split creates child
*places* automatically; their wikis appear only when somebody with a pin there
creates one, and nest under the superseded parent's wiki through ordinary
lineage reconciliation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant, PlaceKind, PlaceRelation, PlaceStatus
from urbanlens.dashboard.services.places import lineage, resolution

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

logger = logging.getLogger(__name__)

#: How much of a parcel's area has to disappear before a refresh is treated as
#: a subdivision rather than a boundary correction. Providers routinely nudge
#: outlines by a few percent; losing a third of a parcel is a different kind of
#: event. Tuning, not design - processing is grandfather-safe either way.
SPLIT_SHRINK_RATIO = 0.66


def looks_like_a_split(place: Place, new_geometry: MultiPolygon | None) -> bool:
    """Whether refreshed geometry suggests this parcel was subdivided.

    Args:
        place: The parcel whose geometry is being refreshed.
        new_geometry: What the provider now returns for the same coordinate.

    Returns:
        True when the parcel has shrunk past :data:`SPLIT_SHRINK_RATIO`.
    """
    if place.kind != PlaceKind.PARCEL or place.geometry is None or new_geometry is None:
        return False
    try:
        previous, current = place.geometry.area, new_geometry.area
    except (AttributeError, TypeError):
        return False
    if previous <= 0:
        return False
    return (current / previous) < (1 - SPLIT_SHRINK_RATIO)


@transaction.atomic
def process_split(place: Place, successors: list[MultiPolygon]) -> Place:
    """Retire a parcel in favour of the parcels it was divided into.

    Args:
        place: The parcel being superseded.
        successors: Official outlines for the new parcels. Fewer than two is a
            correction, not a split, and is rejected by the caller.

    Returns:
        The superseded place.
    """
    from urbanlens.dashboard.services.places.provisioning import upsert_place

    holders = set(
        Place.objects.filter(pk=place.pk).values_list("locations__pins__profile_id", flat=True),
    ) | set(
        Place.objects.filter(domain_root_id=place.domain_root_id).values_list("locations__pins__profile_id", flat=True),
    )
    holders.discard(None)

    children: list[Place] = []
    for geometry in successors:
        # exclude_pk=place.pk: place is still CURRENT here (status flips below,
        # only once we know this is really a split) - without it, a successor
        # whose centroid falls near the original parcel's own centroid (common:
        # the original center often lands inside one of its own pieces) would
        # silently alias back onto place itself instead of becoming a new child.
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
    # no longer prove what these profiles already had. Covers the whole
    # family (place + every successor), not just place - see module docstring.
    PlaceAccessGrant.objects.snapshot_family(holders, place)

    for child in children:
        if child.geometry is not None:
            resolution.resolve_locations_in(child.geometry)
    lineage.refresh_derived_flags([place.pk, *[child.pk for child in children]])

    logger.info("process_split: place %s superseded by %s successors; %s grants snapshotted", place.pk, len(children), len(holders))
    return place
