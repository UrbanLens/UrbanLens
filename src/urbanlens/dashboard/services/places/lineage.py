"""Place lineage - attaching places to each other and keeping domains correct.

Every write that changes a ``Place``'s parent or relation goes through
:func:`set_parent`, because two derived columns have to move with it:

``domain_root``
    The denormalised root of the access domain (see
    :class:`~urbanlens.dashboard.models.place.model.PlaceRelation`). Access
    checks are an indexed equality test on this column instead of a recursive
    walk, which is only sound while it is exact - and it is exact only if every
    re-parent repropagates it down the whole ``PART_OF`` subtree.

``is_aggregate``
    Whether a place has ``MEMBER_OF`` children, which excludes it from point
    resolution. Maintained on both endpoints of an edge, so a place stops being
    an aggregate again the moment its last member detaches.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Count

from urbanlens.dashboard.models.place.model import Place, PlaceRelation

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Hard ceiling on how far lineage walks follow parents. Real lineage is two or
#: three deep (site -> parcel -> building); this only exists so a cycle
#: introduced by a bad migration degrades into a logged error rather than a
#: hung request.
MAX_LINEAGE_DEPTH = 32


class PlaceLineageError(Exception):
    """Raised when an edge would corrupt the place hierarchy."""


def ancestors_of(place: Place) -> list[Place]:
    """Parent, grandparent, ... for a place, nearest first.

    Args:
        place: The place whose ancestry to walk.

    Returns:
        The ancestor chain, empty for a root place. Stops at
        :data:`MAX_LINEAGE_DEPTH` so a pre-existing cycle can't hang a caller.
    """
    chain: list[Place] = []
    seen: set[int] = {place.pk} if place.pk else set()
    current = place.parent if place.parent_id else None
    while current is not None and len(chain) < MAX_LINEAGE_DEPTH:
        if current.pk in seen:
            logger.error("Cycle in place lineage at place %s", current.pk)
            break
        seen.add(current.pk)
        chain.append(current)
        current = current.parent if current.parent_id else None
    return chain


def would_create_cycle(place: Place, new_parent: Place | None) -> bool:
    """Whether attaching ``place`` under ``new_parent`` would form a cycle.

    Args:
        place: The place being re-parented.
        new_parent: The proposed parent, or None to detach.

    Returns:
        True when ``place`` is ``new_parent`` or already one of its ancestors.
    """
    if new_parent is None or place.pk is None:
        return False
    if new_parent.pk == place.pk:
        return True
    return any(ancestor.pk == place.pk for ancestor in ancestors_of(new_parent))


@transaction.atomic
def set_parent(place: Place, parent: Place | None, relation: str = PlaceRelation.PART_OF) -> Place:
    """Attach a place to a parent (or detach it) and repair the derived columns.

    Args:
        place: The place to re-parent. Must be saved.
        parent: The new parent, or None to make ``place`` a domain root.
        relation: How it attaches - ``PART_OF`` keeps both in one access
            domain, ``MEMBER_OF`` makes the parent an earned aggregate.

    Returns:
        The updated place.

    Raises:
        PlaceLineageError: If the edge would create a cycle.
    """
    if place.pk is None:
        raise PlaceLineageError("Cannot re-parent an unsaved place.")
    if would_create_cycle(place, parent):
        raise PlaceLineageError(f"Attaching place {place.pk} under {parent.pk if parent else None} would create a cycle.")

    previous_parent_id = place.parent_id
    place.parent = parent
    place.parent_relation = relation
    place.domain_root_id = parent.domain_root_id if (parent is not None and relation == PlaceRelation.PART_OF) else place.pk
    place.save(update_fields=["parent", "parent_relation", "domain_root", "updated"])

    propagate_domain_root(place)
    refresh_derived_flags([previous_parent_id, parent.pk if parent else None])
    return place


def propagate_domain_root(place: Place) -> int:
    """Push a place's domain root down through its ``PART_OF`` subtree.

    ``MEMBER_OF`` children are left alone by design: each roots its own domain,
    and that boundary is exactly what the recursion must stop at.

    Args:
        place: The place whose subtree to repair. Its own ``domain_root`` is
            taken as already correct.

    Returns:
        How many descendant rows were updated.
    """
    updated = 0
    roots: dict[int, int | None] = {place.pk: place.domain_root_id}
    depth = 0
    while roots and depth < MAX_LINEAGE_DEPTH:
        children = list(Place.objects.filter(parent_id__in=list(roots), parent_relation=PlaceRelation.PART_OF).values("pk", "parent_id", "domain_root_id"))
        if not children:
            break
        next_roots: dict[int, int | None] = {}
        stale: dict[int | None, list[int]] = {}
        for child in children:
            root_id = roots.get(child["parent_id"])
            next_roots[child["pk"]] = root_id
            if child["domain_root_id"] != root_id:
                stale.setdefault(root_id, []).append(child["pk"])
        for root_id, pks in stale.items():
            updated += Place.objects.filter(pk__in=pks).update(domain_root_id=root_id)
        roots = next_roots
        depth += 1
    if depth >= MAX_LINEAGE_DEPTH:
        logger.error("Domain-root propagation from place %s hit the depth ceiling; lineage may contain a cycle", place.pk)
    return updated


def refresh_derived_flags(place_ids: Iterable[int | None]) -> None:
    """Recompute the cached child summaries on the given places.

    ``is_aggregate`` (has ``MEMBER_OF`` children, therefore earned rather than
    resolved onto) and ``building_child_count`` (how many buildings stand on
    it, therefore whether markers here have to commit to parcel-or-building
    scope) are both read on hot paths, so they are maintained on every edge
    change rather than counted per request.

    Args:
        place_ids: Place primary keys; None entries and duplicates are ignored.
    """
    from urbanlens.dashboard.models.place.model import PlaceKind

    unique_ids = {pk for pk in place_ids if pk is not None}
    if not unique_ids:
        return

    with_members = set(Place.objects.filter(parent_id__in=unique_ids, parent_relation=PlaceRelation.MEMBER_OF).values_list("parent_id", flat=True))
    for flag, ids in ((True, unique_ids & with_members), (False, unique_ids - with_members)):
        if ids:
            Place.objects.filter(pk__in=ids).exclude(is_aggregate=flag).update(is_aggregate=flag)

    counts = dict(
        Place.objects.filter(parent_id__in=unique_ids, parent_relation=PlaceRelation.PART_OF, kind=PlaceKind.BUILDING)
        .values_list("parent_id")
        .annotate(total=Count("pk"))
        .values_list("parent_id", "total"),
    )
    by_count: dict[int, list[int]] = {}
    for pk in unique_ids:
        by_count.setdefault(counts.get(pk, 0), []).append(pk)
    for total, pks in by_count.items():
        Place.objects.filter(pk__in=pks).exclude(building_child_count=total).update(building_child_count=total)


def member_children(place: Place) -> list[Place]:
    """The places that must *all* be held to earn access to ``place``.

    Args:
        place: The aggregate (or superseded) place.

    Returns:
        Its ``MEMBER_OF`` children, empty when it is not earned this way.
    """
    return list(place.children.filter(parent_relation=PlaceRelation.MEMBER_OF))
