"""
Nesting a profile's duplicate root pins on one property, after the property is known.

A pin only learns which property it stands on once its Location resolves onto a place, which for imports happens
after the pin exists. When that happens, a profile's newer root pin on a property it already has a root pin on is
nested under the oldest one. Pins are never merged, deleted or moved, so nothing is lost and the owner can undo it.

Only the profile's own pins are read or changed.

TODO(UL-363): rerun the pass for a profile when the boundary-shrink code returns pins to root; that code is not on this base.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Case, Count, IntegerField, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceStatus
from urbanlens.dashboard.services.pins.tree_lock import lock_profile_pin_tree

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from django.db.models import QuerySet

logger = logging.getLogger(__name__)

#: Locations looked up per query, so a very large import stays within the parameter limits.
_CHUNK = 2000


@dataclass(frozen=True, slots=True)
class NestedPin:
    """One root pin that was nested under an older root pin on the same property."""

    profile_id: int
    pin_id: int
    pin_name: str | None
    parent_id: int
    parent_name: str | None


_deferred_locations: ContextVar[set[int] | None] = ContextVar("property_nesting_deferred", default=None)


def _domain_key() -> Coalesce:
    """The property a root pin stands on: its place's domain root, or the place itself before the root is stamped."""
    return Coalesce("location__place__domain_root_id", "location__place_id")


def nest_duplicate_roots(*, profile_id: int | None = None, location_ids: Iterable[int] | None = None) -> list[NestedPin]:
    """
    Nest each profile's newer root pins under its oldest root pin on the same property.

    Pass ``location_ids`` for the Locations that just gained a place, or ``profile_id`` to sweep one profile. The oldest root is the target; it may be one the user made deliberately, but a root the user made deliberately (``Pin.parenting_is_manual``) is never moved. Descendants move with their parent, and a move is skipped when the target is no longer a root.

    Args:
        profile_id: Sweep every property this profile has a root pin on.
        location_ids: Sweep the properties these Locations stand on, for every profile with a root pin on them.

    Returns:
        What was nested; empty when nothing needed to be, so a second run changes nothing.
    """
    # TODO(UL-365): only places with official/footprint evidence should count as a property; until Place.evidence exists, every current place qualifies.
    pairs = _candidate_pairs(profile_id, location_ids)
    nested: list[NestedPin] = []
    by_profile: dict[int, set[int]] = defaultdict(set)
    for owner_id, domain_id in pairs:
        by_profile[owner_id].add(domain_id)
    for owner_id in sorted(by_profile):
        nested.extend(_nest_for_profile(owner_id, by_profile[owner_id]))
    return nested


def _candidate_pairs(profile_id: int | None, location_ids: Iterable[int] | None) -> set[tuple[int, int]]:
    """The (profile, property) pairs holding more than one root pin, found before any pin is loaded."""
    base = Pin.objects.root_pins().filter(location__place__status=PlaceStatus.CURRENT).annotate(domain=_domain_key()).filter(domain__isnull=False)
    if profile_id is not None:
        return _duplicated(base.filter(profile_id=profile_id))
    ids = sorted(set(location_ids or ()))
    pairs: set[tuple[int, int]] = set()
    for start in range(0, len(ids), _CHUNK):
        touched = base.filter(location_id__in=ids[start : start + _CHUNK]).values_list("profile_id", "domain").distinct()
        # Counted over everything the profile has on that property, not only the touched Locations.
        for owner_id, domain_id in set(touched):
            pairs.update(_duplicated(base.filter(profile_id=owner_id, domain=domain_id)))
    return pairs


def _duplicated(roots: QuerySet[Pin]) -> set[tuple[int, int]]:
    """The (profile, property) pairs among *roots* with more than one pin."""
    grouped = roots.order_by().values("profile_id", "domain").annotate(n=Count("pk")).filter(n__gt=1)
    return {(row["profile_id"], row["domain"]) for row in grouped}


def _nest_for_profile(profile_id: int, domain_ids: set[int]) -> list[NestedPin]:
    """Nest one profile's duplicate roots in the given properties, under that profile's lock."""
    from urbanlens.dashboard.services.geo.child_pin_boundaries import deferring_child_boundary_refits, request_child_boundary_refit

    with transaction.atomic(), deferring_child_boundary_refits():
        lock_profile_pin_tree(profile_id)
        roots: list[dict[str, Any]] = [
            dict(row)
            for row in (
                Pin.objects.root_pins()
                .filter(profile_id=profile_id, location__place__status=PlaceStatus.CURRENT)
                .annotate(domain=_domain_key())
                .filter(domain__in=domain_ids)
                .order_by("created", "pk")
                .values("pk", "name", "parenting_is_manual", "domain")
            )
        ]
        groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in roots:
            groups[row["domain"]].append(row)
        plan: dict[int, dict[str, Any]] = {}
        for rows in groups.values():
            if len(rows) < 2:
                continue
            target = rows[0]
            for row in rows[1:]:
                if not row["parenting_is_manual"]:
                    plan[row["pk"]] = {"row": row, "target": target}
        if not plan:
            return []

        # Movers and targets locked together in pk order, then re-checked: a target that has since been nested,
        # or one lying beneath its mover, would turn the move into a cycle.
        wanted = set(plan) | {entry["target"]["pk"] for entry in plan.values()}
        locked = {pk: (parent, manual) for pk, parent, manual in Pin.objects.select_for_update(of=("self",)).filter(pk__in=wanted, profile_id=profile_id).order_by("pk").values_list("pk", "parent_pin_id", "parenting_is_manual")}
        moves: dict[int, dict[str, Any]] = {}
        for pk, entry in plan.items():
            target_pk = entry["target"]["pk"]
            mover, held_target = locked.get(pk), locked.get(target_pk)
            if mover is None or held_target is None or target_pk == pk or mover[0] is not None or mover[1] or held_target[0] is not None:
                continue
            moves[pk] = entry
        if not moves:
            return []
        Pin.objects.filter(pk__in=moves).update(
            parent_pin_id=Case(*[When(pk=pk, then=Value(entry["target"]["pk"])) for pk, entry in moves.items()], output_field=IntegerField()),
            updated=timezone.now(),
        )
        for parent_id in {entry["target"]["pk"] for entry in moves.values()}:
            request_child_boundary_refit(parent_id)
    return [NestedPin(profile_id=profile_id, pin_id=pk, pin_name=entry["row"]["name"], parent_id=entry["target"]["pk"], parent_name=entry["target"]["name"]) for pk, entry in sorted(moves.items())]


def request_property_nesting(location_id: int) -> None:
    """
    Check the property a Location just gained a place on, once the surrounding transaction commits.

    Inside :func:`deferring_property_nesting` the check is held until the block ends, so a batch pays for one pass.
    """
    pending = _deferred_locations.get()
    if pending is not None:
        pending.add(location_id)
        return
    transaction.on_commit(lambda: _run_quietly(location_ids=[location_id]))


@contextmanager
def deferring_property_nesting() -> Iterator[list[NestedPin]]:
    """
    Collect the Locations that gain a place in this block and nest their properties' duplicate roots once, when it ends.

    Yields:
        A list filled with what was nested when the block ends (empty while it runs). Nested blocks defer to the outermost.
    """
    results: list[NestedPin] = []
    if _deferred_locations.get() is not None:
        yield results
        return
    pending: set[int] = set()
    token = _deferred_locations.set(pending)
    try:
        yield results
    finally:
        _deferred_locations.reset(token)
        if pending:
            results.extend(_run_quietly(location_ids=pending))


def _run_quietly(*, location_ids: Iterable[int] | None = None, profile_id: int | None = None, notify: bool = True) -> list[NestedPin]:
    """Run the pass, logging instead of raising: a failed tidy-up must not fail the resolution or import that triggered it."""
    try:
        nested = nest_duplicate_roots(location_ids=location_ids, profile_id=profile_id)
    except Exception:
        logger.exception("Property nesting failed")
        return []
    if nested:
        logger.info("Nested %s duplicate root pin(s) under older pins on the same property", len(nested))
        if notify:
            _notify_owners(nested)
    return nested


def sweep_after_import(profile_id: int) -> list[NestedPin]:
    """Nest a profile's duplicate roots at the end of an import, whose summary reports them instead of a notification."""
    return _run_quietly(profile_id=profile_id, notify=False)


NOTIFIED_NAMES = 10


def _notify_owners(nested: Sequence[NestedPin]) -> None:
    """Tell each owner, once, which of their own pins were nested; nothing about anyone else's."""
    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    by_profile: dict[int, list[NestedPin]] = defaultdict(list)
    for item in nested:
        by_profile[item.profile_id].append(item)
    for owner_id, items in by_profile.items():
        names = ", ".join(f"'{item.pin_name or 'Unnamed pin'}'" for item in items[:NOTIFIED_NAMES])
        more = f" and {len(items) - NOTIFIED_NAMES} more" if len(items) > NOTIFIED_NAMES else ""
        try:
            NotificationLog.objects.notify(
                profile_id=owner_id,
                status=Status.UNREAD,
                importance=Importance.LOW,
                notification_type=NotificationType.INFO,
                title="Pins nested on the same property",
                message=f"{len(items)} of your pins stand on a property you already had a pin on, so they were nested under the older pin: {names}{more}. Detach a pin to undo this.",
            )
        except Exception:
            logger.exception("Could not notify profile %s of nested pins", owner_id)


def request_property_nesting_many(location_ids: Iterable[int]) -> None:
    """:func:`request_property_nesting` for several Locations at once."""
    for location_id in location_ids:
        request_property_nesting(location_id)


MAX_NAMES_IN_SUMMARY = 25


def describe_nested(nested: Sequence[NestedPin]) -> dict[str, object]:
    """
    A structured summary of what was nested, for an import's result.

    Args:
        nested: What :func:`nest_duplicate_roots` returned for one profile.

    Returns:
        ``count``, and ``pins`` listing up to :data:`MAX_NAMES_IN_SUMMARY` of ``{"name", "parent_name"}``.
    """
    return {"count": len(nested), "pins": [{"name": item.pin_name, "parent_name": item.parent_name} for item in nested[:MAX_NAMES_IN_SUMMARY]]}


def summary_warning(nested: Sequence[NestedPin]) -> str | None:
    """One line for an import summary naming what was nested, or None when nothing was."""
    if not nested:
        return None
    names = ", ".join(f"'{item.pin_name or 'Unnamed pin'}'" for item in nested[:MAX_NAMES_IN_SUMMARY])
    more = f" and {len(nested) - MAX_NAMES_IN_SUMMARY} more" if len(nested) > MAX_NAMES_IN_SUMMARY else ""
    noun = "pin" if len(nested) == 1 else "pins"
    return f"Nested {len(nested)} {noun} under an older pin on the same property: {names}{more}. Detach one to keep it separate."


__all__ = [
    "MAX_NAMES_IN_SUMMARY",
    "NestedPin",
    "deferring_property_nesting",
    "describe_nested",
    "nest_duplicate_roots",
    "request_property_nesting",
    "request_property_nesting_many",
    "summary_warning",
    "sweep_after_import",
]
