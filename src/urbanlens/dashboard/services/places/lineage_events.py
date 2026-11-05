"""The one place where a change in what a place is turns into permanent access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - read by dataclass consumers at runtime
from enum import StrEnum
import logging
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class LineageEventKind(StrEnum):
    """What happened to the predecessors."""

    SPLIT = "split"
    MERGE = "merge"
    RECLASSIFY = "reclassify"


_REASONS = {
    LineageEventKind.SPLIT: GrantReason.GRANDFATHERED_SPLIT,
    LineageEventKind.MERGE: GrantReason.GRANDFATHERED_LINEAGE,
    LineageEventKind.RECLASSIFY: GrantReason.GRANDFATHERED_LINEAGE,
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


@transaction.atomic
def handle_lineage_event(event: LineageEvent) -> int:
    """
    Snapshot the holders of the predecessors' domains onto the predecessors, their families and the successors.
    Grants are only ever added here.

    Args:
        event: What happened.

    Returns:
        The number of profiles granted.
    """
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
