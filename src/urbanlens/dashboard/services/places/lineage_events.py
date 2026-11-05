"""The one place where a change in what a place is turns into permanent access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - read by dataclass consumers at runtime
from enum import StrEnum
import logging
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.place.lifecycle import LineageEdgeKind, PlaceLineageEdge
from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class LineageEventKind(StrEnum):
    """What happened to the predecessors."""

    SPLIT = "split"
    MERGE = "merge"
    RECLASSIFY = "reclassify"
    REPLACE = "replace"


_REASONS = {
    LineageEventKind.SPLIT: GrantReason.GRANDFATHERED_SPLIT,
    LineageEventKind.MERGE: GrantReason.GRANDFATHERED_LINEAGE,
    LineageEventKind.RECLASSIFY: GrantReason.GRANDFATHERED_LINEAGE,
    LineageEventKind.REPLACE: GrantReason.GRANDFATHERED_LINEAGE,
}

_EDGE_KINDS = {
    LineageEventKind.SPLIT: LineageEdgeKind.SPLIT_INTO,
    LineageEventKind.MERGE: LineageEdgeKind.MERGED_INTO,
    LineageEventKind.RECLASSIFY: LineageEdgeKind.REPLACED_BY,
    LineageEventKind.REPLACE: LineageEdgeKind.REPLACED_BY,
}


@dataclass(frozen=True, slots=True)
class LineageEvent:
    """
    A split, merge or reclassification, whoever detected it.

    Attributes:
        kind: What happened.
        predecessors: Places that stopped describing the ground as they did.
        successors: Places that now describe it.
        learned_at: When the event was recorded. Holders are those with a pin made by then; a pin made later
            was made knowing the new shape and earns nothing from the old one.
    """

    kind: LineageEventKind
    predecessors: Sequence[Place]
    successors: Sequence[Place]
    learned_at: datetime


def holders_of(places: Sequence[Place], as_of: datetime) -> set[int]:
    """
    Profiles that held the access domains these places sit in.

    Args:
        places: Places whose domains to read.
        as_of: Ignore pins created after this.

    Returns:
        Profile ids.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    roots = {place.domain_root_id for place in places if place.domain_root_id is not None}
    if not roots:
        return set()
    return set(
        Pin.objects.filter(location__place__domain_root_id__in=roots, created__lte=as_of).values_list("profile_id", flat=True).distinct(),
    )


def _record_edges(event: LineageEvent) -> None:
    """Write one edge per predecessor and successor, and close the predecessors' validity."""
    kind = _EDGE_KINDS[event.kind]
    for predecessor in event.predecessors:
        for successor in event.successors:
            if successor.pk == predecessor.pk:
                continue
            PlaceLineageEdge.objects.get_or_create(predecessor=predecessor, successor=successor, kind=kind, defaults={"learned_at": event.learned_at})
        Place.objects.filter(pk=predecessor.pk, valid_to__isnull=True).update(valid_to=event.learned_at)
        predecessor.valid_to = predecessor.valid_to or event.learned_at


def repoint_pin_subjects(event: LineageEvent) -> None:
    """
    Hook for moving ``Pin.subject_place`` from a predecessor to the successor containing the pin's point.

    ADR34 §5 makes the lineage handler the only writer of that field. It does nothing until UL-361 (``Pin.subject_place``) lands.
    """
    # TODO(UL-361): repoint Pin.subject_place here, for pins whose subject is in event.predecessors.


def retract_erroneous_event(edges: Sequence[PlaceLineageEdge], reason: str) -> int:
    """
    Undo the access an event gave, because the event itself was a mistake.

    This is the only path that removes a grant, and it is for corrections flagged erroneous. A profile keeps any
    grant on a place whose domain it still has a pin in, since containment proves that access without the grant.

    Args:
        edges: The edges the mistaken event wrote.
        reason: Why the event was wrong; logged.

    Returns:
        The number of grants removed.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    removed: list[tuple[int, int]] = []
    with transaction.atomic():
        for edge in edges:
            reasons = [GrantReason.GRANDFATHERED_SPLIT, GrantReason.GRANDFATHERED_LINEAGE]
            holders = holders_of([edge.predecessor], edge.learned_at)
            targets = [edge.successor, *PlaceAccessGrant.objects.family_of(edge.successor)]
            for grant in PlaceAccessGrant.objects.filter(profile_id__in=holders, place__in=targets, reason__in=reasons):
                if Pin.objects.filter(profile_id=grant.profile_id, location__place__domain_root_id=grant.place.domain_root_id).exists():
                    continue
                removed.append((grant.profile_id, grant.place_id))
                grant.delete()
        PlaceLineageEdge.objects.filter(pk__in=[edge.pk for edge in edges]).update(erroneous=True)
    logger.warning("erroneous lineage event retracted (%s): removed %s grants (profile, place): %s", reason, len(removed), removed)
    return len(removed)


@transaction.atomic
def handle_lineage_event(event: LineageEvent) -> int:
    """
    Record the lineage edges, then snapshot the holders of the predecessors' domains onto the predecessors, their families and the successors.
    Grants are only ever added here.

    Args:
        event: What happened.

    Returns:
        The number of profiles granted.
    """
    _record_edges(event)
    repoint_pin_subjects(event)
    holders = holders_of(event.predecessors, event.learned_at)
    if not holders:
        return 0
    targets: list[Place] = []
    for predecessor in event.predecessors:
        targets.extend(PlaceAccessGrant.objects.family_of(predecessor))
    targets.extend(event.successors)
    PlaceAccessGrant.objects.grant_places(holders, targets, reason=_REASONS[event.kind])
    logger.info("lineage %s: %s holders granted across %s places", event.kind, len(holders), len(targets))
    return len(holders)
