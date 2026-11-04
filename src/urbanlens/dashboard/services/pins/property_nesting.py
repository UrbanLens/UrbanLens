"""
Nesting a profile's duplicate root pins on one property, after the property is known.

A pin only learns which property it stands on once its Location resolves onto a place, which for imports happens
after the pin exists. When that happens, a profile's newer root pin on a property it already has a root pin on is
nested under the oldest one. Pins are never merged, deleted or moved, so nothing is lost and the owner can undo it.

Only the profile's own pins are read or changed.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.db.models import Case, IntegerField, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)

#: First key of ``pg_advisory_xact_lock(key, profile_id)``; keeps this lock apart from other advisory locks.
ADVISORY_LOCK_NAMESPACE = 357_001

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
    """The (profile, property) pairs worth checking, from at most one query per chunk of Locations."""
    base = Pin.objects.root_pins().annotate(domain=_domain_key()).filter(domain__isnull=False)
    if profile_id is not None:
        return set(base.filter(profile_id=profile_id).values_list("profile_id", "domain").distinct())
    ids = sorted(set(location_ids or ()))
    pairs: set[tuple[int, int]] = set()
    for start in range(0, len(ids), _CHUNK):
        pairs.update(base.filter(location_id__in=ids[start : start + _CHUNK]).values_list("profile_id", "domain").distinct())
    return pairs


def _nest_for_profile(profile_id: int, domain_ids: set[int]) -> list[NestedPin]:
    """Nest one profile's duplicate roots in the given properties, under that profile's lock."""
    from urbanlens.dashboard.services.geo.child_pin_boundaries import deferring_child_boundary_refits, request_child_boundary_refit

    with transaction.atomic(), deferring_child_boundary_refits():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, profile_id])
        roots = list(Pin.objects.root_pins().filter(profile_id=profile_id).annotate(domain=_domain_key()).filter(domain__in=domain_ids).order_by("created", "pk").values("pk", "name", "parenting_is_manual", "domain"))
        groups: dict[int, list[dict]] = defaultdict(list)
        for row in roots:
            groups[row["domain"]].append(row)
        plan: dict[int, dict] = {}
        for rows in groups.values():
            if len(rows) < 2:
                continue
            target = rows[0]
            for row in rows[1:]:
                if not row["parenting_is_manual"]:
                    plan[row["pk"]] = {"row": row, "target": target}
        if not plan:
            return []

        # Row locks only on what changes, in pk order, re-checked now that they are held.
        locked = dict(Pin.objects.select_for_update(of=("self",)).filter(pk__in=plan, parent_pin__isnull=True, parenting_is_manual=False).order_by("pk").values_list("pk", "parent_pin_id"))
        targets_still_roots = set(Pin.objects.filter(pk__in={entry["target"]["pk"] for entry in plan.values()}, parent_pin__isnull=True).values_list("pk", flat=True))
        moves = {pk: entry for pk, entry in plan.items() if pk in locked and entry["target"]["pk"] in targets_still_roots and entry["target"]["pk"] != pk}
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


def _run_quietly(*, location_ids: Iterable[int]) -> list[NestedPin]:
    """Run the pass, logging instead of raising: a failed tidy-up must not fail the resolution that triggered it."""
    try:
        nested = nest_duplicate_roots(location_ids=location_ids)
    except Exception:
        logger.exception("Property nesting failed")
        return []
    if nested:
        logger.info("Nested %s duplicate root pin(s) under older pins on the same property", len(nested))
    return nested


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
    "summary_warning",
]
