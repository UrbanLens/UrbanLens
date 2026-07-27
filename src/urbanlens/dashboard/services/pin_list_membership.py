"""Smart PinList membership matching and sync.

A "smart" PinList (``PinList.is_smart=True``) auto-includes pins matching a
saved filter (``smart_filter``, same JSON shape as ``SavedFilter.criteria``)
and/or falling inside a drawn boundary polygon (``smart_boundary``). Two entry
points keep membership current:

- ``sync_pin_against_smart_lists`` - called from a Pin post_save signal
  (see ``dashboard.models.pin_list.signals``) to evaluate one pin against all
  of its owner's smart lists.
- ``resync_smart_list`` - called synchronously from the list-edit view when a
  list's ``smart_filter``/``smart_boundary`` changes (or ``is_smart`` is
  turned on), to fully recompute that one list's membership.

``PinListItem.added_via`` tracks provenance so manually-added pins are never
auto-removed, even if they also happen to match (or stop matching) a smart
rule.

Alongside the smart-membership machinery, this module owns the *explicit*
membership operations (add/remove/reorder). Those were previously written
inline in ``controllers.pin_lists`` and are shared here so the external API
(``external_api.views``) applies byte-for-byte the same cap enforcement,
duplicate skipping and ordering rules the web UI does, rather than a second
implementation that could drift.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Q

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_list.model import PinList
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter


@dataclass(frozen=True)
class ListAddResult:
    """Outcome of one :func:`add_pins_to_list` call.

    Attributes:
        added: How many ``PinListItem`` rows were actually created. Pins
            already on the list are not counted - they are skipped, not
            re-added.
        skipped_over_cap: How many otherwise-addable pins were dropped because
            the list would have exceeded ``max_pins``. Zero when the cap is
            disabled or was never reached.
        max_pins: The cap in force at the time of the call (0 = unlimited), so
            a caller can render an accurate message without re-reading
            ``SiteSettings``.
    """

    added: int
    skipped_over_cap: int
    max_pins: int


def sync_pin_against_smart_lists(pin: Pin) -> None:
    """Evaluate one pin against every smart list owned by the same profile.

    Args:
        pin: The pin that was just created/edited.
    """
    from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem

    smart_lists = PinList.objects.active_smart_lists(pin.profile_id)
    for pin_list in smart_lists:
        matches = _pin_matches_smart_list(pin, pin_list)
        existing = PinListItem.objects.membership(pin_list, pin)
        if matches and existing is None:
            # Overlapping Pin.save() transactions can both see "no existing
            # membership" and race to create one - the table has a
            # UniqueConstraint(pin_list, pin), so the loser here just means
            # the concurrent path already added it; treat that as a no-op
            # rather than letting IntegrityError bubble up as a 500.
            with contextlib.suppress(IntegrityError):
                PinListItem.objects.create(
                    pin_list=pin_list,
                    pin=pin,
                    order=pin_list.items.count(),
                    added_via=_provenance(pin, pin_list),
                )
        elif not matches and existing is not None and existing.added_via != PinListItem.ADDED_MANUAL:
            existing.delete()
        # matches + manual, or not-matches + manual: no-op either way - manual always wins.


def resync_smart_list(pin_list: PinList, *, filter_ids: set[int] | None = None) -> None:
    """Fully recompute one list's membership against its current smart_filter/smart_boundary rules.

    Callable regardless of ``is_smart`` - picking a filter/boundary should show
    a matching snapshot right away, even before the user opts into ongoing
    auto-sync. ``is_smart`` only gates whether *future* pin edits keep
    re-triggering this (see ``sync_pin_against_smart_lists``, wired to Pin's
    post_save/labels-m2m signals).

    Newly-added items never push the list past ``SiteSettings.get_current().
    max_pins_per_list`` (0 = unlimited) - matches already on the list are kept
    even if the cap is already met/exceeded (e.g. the cap was lowered after
    the fact), but no *new* items are added beyond it. When more candidates
    need adding than remain under the cap, which ones get kept is an
    arbitrary-but-deterministic choice (lowest pin id first), so re-running a
    resync against the same rules is reproducible.

    Args:
        pin_list: The list whose ``smart_filter``/``smart_boundary`` just changed.
        filter_ids: Precomputed result of ``filter_matching_ids(pin_list)``,
            for callers that already resolved it (e.g. resyncing several
            lists that share the exact same freshly-saved ``smart_filter``
            criteria) and want to skip re-resolving the same Label/CustomField
            criteria into a Pin queryset for every list. Resolved internally
            when omitted.
    """
    from urbanlens.dashboard.models.pin_list.model import PinListItem
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    resolved_filter_ids = filter_matching_ids(pin_list) if filter_ids is None else filter_ids
    boundary_ids = _boundary_matching_ids(pin_list)
    candidate_ids = resolved_filter_ids | boundary_ids

    current = {item.pin_id: item for item in pin_list.items.all()}
    to_remove = [pk for pk, item in current.items() if pk not in candidate_ids and item.added_via != PinListItem.ADDED_MANUAL]
    if to_remove:
        PinListItem.objects.for_list(pin_list).filter(pin_id__in=to_remove).delete()

    base_order = len(current) - len(to_remove)
    to_add = sorted(candidate_ids - current.keys())
    if not to_add:
        return

    max_pins = SiteSettings.get_current().max_pins_per_list
    if max_pins > 0:
        remaining = max(0, max_pins - base_order)
        if remaining < len(to_add):
            to_add = to_add[:remaining]
    if not to_add:
        return

    PinListItem.objects.bulk_create(
        [
            PinListItem(
                pin_list=pin_list,
                pin_id=pk,
                order=base_order + i,
                added_via=PinListItem.ADDED_SMART_FILTER if pk in resolved_filter_ids else PinListItem.ADDED_BOUNDARY,
            )
            for i, pk in enumerate(to_add)
        ],
    )


def _pin_matches_smart_list(pin: Pin, pin_list: PinList) -> bool:
    if pin_list.smart_filter and _pin_matches_filter(pin, pin_list):
        return True
    return bool(pin_list.smart_boundary and _pin_in_boundary(pin, pin_list))


def _provenance(pin: Pin, pin_list: PinList) -> str:
    from urbanlens.dashboard.models.pin_list.model import PinListItem

    if pin_list.smart_filter and _pin_matches_filter(pin, pin_list):
        return PinListItem.ADDED_SMART_FILTER
    return PinListItem.ADDED_BOUNDARY


def _pin_matches_filter(pin: Pin, pin_list: PinList) -> bool:
    smart_filter = pin_list.smart_filter
    if not smart_filter:
        return False
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.filter_criteria import deserialize_criteria

    criteria = deserialize_criteria(smart_filter, pin_list.profile)
    return Pin.objects.filter(pk=pin.pk).filter_by_criteria(criteria).exists()


def _pin_in_boundary(pin: Pin, pin_list: PinList) -> bool:
    if pin.location_id is None:
        return False
    from urbanlens.dashboard.models.location.model import Location

    return Location.objects.filter(pk=pin.location_id, point__within=pin_list.smart_boundary).exists()


def filter_matching_ids(pin_list: PinList) -> set[int]:
    """Resolve ``pin_list.smart_filter`` into the set of currently-matching pin ids.

    Exposed publicly (not just an internal helper of ``resync_smart_list``) so
    callers resyncing several lists that share the exact same freshly-saved
    ``smart_filter`` criteria and profile (e.g. every ``PinList`` derived from
    one edited ``SavedFilter``) can resolve it once and pass the result to
    each list's ``resync_smart_list(pin_list, filter_ids=...)`` call, instead
    of every list independently re-resolving the same Label/CustomField
    criteria into a Pin queryset.

    Args:
        pin_list: The list whose ``smart_filter`` to resolve.

    Returns:
        Set of matching pin ids, or an empty set when there's no smart_filter.
    """
    if not pin_list.smart_filter:
        return set()
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.filter_criteria import deserialize_criteria

    criteria = deserialize_criteria(pin_list.smart_filter, pin_list.profile)
    return set(Pin.objects.filter(profile=pin_list.profile).filter_by_criteria(criteria).values_list("pk", flat=True))


def add_pins_to_list(pin_list: PinList, pins: Sequence[Pin], *, added_via: str | None = None) -> ListAddResult:
    """Add *pins* to *pin_list*, skipping duplicates and honoring the per-list cap.

    Extracted verbatim from ``controllers.pin_lists.PinListAddPinsView.post``
    so the web UI and the external API share one implementation. Three rules
    apply, in this order:

    1. Pins already on the list are dropped - the table carries
       ``UniqueConstraint(pin_list, pin)`` (``uq_pin_list_item``), so
       re-adding one would raise rather than no-op.
    2. New items are appended, numbered from the list's current item count, so
       they land after everything already there.
    3. ``SiteSettings.get_current().max_pins_per_list`` (0 = unlimited) bounds
       the result. Pins over the cap are silently dropped and reported via
       ``skipped_over_cap`` rather than raising - a partial add is the
       behavior the UI already relies on.

    The whole thing runs in one transaction: the count that determines
    ordering and remaining capacity is read in the same atomic block that
    creates the rows.

    Args:
        pin_list: The list to add to.
        pins: The pins to add. Callers are responsible for having scoped these
            to the list owner's own pins - this function does not re-check
            ownership.
        added_via: Provenance stamped on the new rows. Defaults to
            ``PinListItem.ADDED_MANUAL``, which is what protects them from
            ever being auto-removed by a later smart-list resync. Passed as
            ``None`` rather than referencing ``PinListItem`` in the signature
            so this module keeps its function-level model imports (see the
            other functions here) and stays import-cycle free.

    Returns:
        A :class:`ListAddResult` describing what happened.
    """
    from urbanlens.dashboard.models.pin_list.model import PinListItem
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    provenance = PinListItem.ADDED_MANUAL if added_via is None else added_via
    max_pins = SiteSettings.get_current().max_pins_per_list

    with transaction.atomic():
        existing_pin_ids = set(pin_list.items.values_list("pin_id", flat=True))
        # Preserves caller order while de-duplicating a payload that names the
        # same pin twice - without this, two rows for one pin would violate
        # uq_pin_list_item inside bulk_create.
        new_pins: list[Pin] = []
        seen: set[int] = set()
        for pin in pins:
            if pin.pk in existing_pin_ids or pin.pk in seen:
                continue
            seen.add(pin.pk)
            new_pins.append(pin)

        base_order = len(existing_pin_ids)
        skipped_over_cap = 0
        if max_pins > 0:
            remaining = max(0, max_pins - base_order)
            if len(new_pins) > remaining:
                skipped_over_cap = len(new_pins) - remaining
                new_pins = new_pins[:remaining]

        if new_pins:
            PinListItem.objects.bulk_create(
                [PinListItem(pin_list=pin_list, pin=pin, order=base_order + i, added_via=provenance) for i, pin in enumerate(new_pins)],
            )

    return ListAddResult(added=len(new_pins), skipped_over_cap=skipped_over_cap, max_pins=max_pins)


def remove_pins_from_list(pin_list: PinList, pin_ids: Sequence[int]) -> int:
    """Remove the given pins from *pin_list*, whatever their provenance.

    Explicit removal always wins - a smart-filter or boundary match is removed
    just like a manual one. (A later resync may of course re-add it if it
    still matches the list's rules; that is the same behavior the web UI has.)

    Deliberately does *not* renumber the remaining items. ``PinListItem.Meta.
    ordering = ["order", "created"]`` tolerates gaps, so closing them would be
    a full-table rewrite for no visible difference.

    Args:
        pin_list: The list to remove from.
        pin_ids: Primary keys of the pins to remove. Ids not on this list are
            ignored.

    Returns:
        How many membership rows were actually deleted.
    """
    from urbanlens.dashboard.models.pin_list.model import PinListItem

    deleted, _ = PinListItem.objects.for_list(pin_list).filter(pin_id__in=list(pin_ids)).delete()
    return deleted


def reorder_list_items(pin_list: PinList, item_ids: Sequence[int]) -> int:
    """Renumber *pin_list*'s items so they follow the order given in *item_ids*.

    Each item's ``order`` becomes its index in *item_ids*. Ids that don't
    belong to this list are ignored rather than rejected, matching
    ``controllers.pin_lists.PinListReorderView``'s existing leniency: the
    drag-and-drop UI can submit a stale id for an item deleted in another tab,
    and failing the whole reorder over that would be worse than skipping it.

    Note that ignored ids still consume their index, so a submission
    containing foreign ids yields a sparse-but-correctly-ordered result rather
    than a contiguous one - again matching the current behavior.

    Args:
        pin_list: The list whose items are being reordered.
        item_ids: ``PinListItem`` primary keys in their new display order.

    Returns:
        How many items were actually renumbered.
    """
    from urbanlens.dashboard.models.pin_list.model import PinListItem

    ids = list(item_ids)
    items_by_id = {item.pk: item for item in PinListItem.objects.for_list(pin_list).filter(pk__in=ids)}

    updated: list[PinListItem] = []
    for order, item_id in enumerate(ids):
        item = items_by_id.get(item_id)
        if item is None:
            continue
        item.order = order
        updated.append(item)
    if updated:
        PinListItem.objects.bulk_update(updated, ["order"])
    return len(updated)


def resync_lists_for_saved_filter(saved_filter: SavedFilter) -> int:
    """Refresh every PinList derived from *saved_filter* against its current criteria.

    ``PinList.smart_filter`` is a one-time *copy* of a SavedFilter's criteria,
    not a live reference, so a list pointed at a filter silently drifts out of
    sync the moment that filter is edited. Every write path that changes
    ``SavedFilter.criteria`` must call this.

    The shared-computation optimization here was extracted from
    ``controllers.saved_filters.SavedFilterEditView.post``: all derived lists
    belong to the same profile and, once the copy below has run, carry
    identical criteria - so the matching pin ids are resolved once and reused,
    instead of every list independently re-resolving the same Label and
    CustomField criteria into a Pin queryset.

    Args:
        saved_filter: The filter whose ``criteria`` just changed.

    Returns:
        How many derived lists were resynced.
    """
    criteria = saved_filter.criteria
    shared_filter_ids: set[int] | None = None
    resynced = 0
    for pin_list in saved_filter.derived_pin_lists.all():
        pin_list.smart_filter = criteria
        pin_list.save(update_fields=["smart_filter", "updated"])
        if shared_filter_ids is None:
            shared_filter_ids = filter_matching_ids(pin_list)
        resync_smart_list(pin_list, filter_ids=shared_filter_ids)
        resynced += 1
    return resynced


def _boundary_matching_ids(pin_list: PinList) -> set[int]:
    if not pin_list.smart_boundary:
        return set()
    from urbanlens.dashboard.models.pin.model import Pin

    return set(
        Pin.objects.filter(profile=pin_list.profile, location__point__within=pin_list.smart_boundary).values_list("pk", flat=True),
    )
