"""Smart PinList membership matching and sync.
``PinListItem.added_via`` tracks provenance so manually-added pins are never auto-removed, even if they also happen to match (or stop matching) a smart rule."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.services.geo.longitude import split_at_antimeridian

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin.queryset import PinQuerySet
    from urbanlens.dashboard.models.pin_list.model import PinList
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter


@dataclass(frozen=True)
class ListAddResult:
    """Outcome of one :func:`add_pins_to_list` call.

    Attributes:
        added: How many ``PinListItem`` rows were actually created.
        skipped_over_cap: How many otherwise-addable pins were dropped because the list would have exceeded ``max_pins``.
        max_pins: The cap in force at the time of the call (0 = unlimited), so a caller can render an accurate message without re-reading ``SiteSettings``."""

    added: int
    skipped_over_cap: int
    max_pins: int


def sync_pins_against_smart_lists(profile_id: int, pin_ids: Collection[int]) -> None:
    """Evaluate some of one account's pins against every smart list it has.

    One query per rule per list, however many pins: each list's filter and boundary are run over the pins together,
    and their memberships are read once. A pin that matches is added with the rule that matched (the filter ahead of
    the boundary); one that stopped matching leaves, unless it was added by hand. Pins no longer the account's, or
    gone, are skipped.

    Args:
        profile_id: The account whose lists and pins these are.
        pin_ids: The pins to evaluate.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem

    pin_lists = list(PinList.objects.active_smart_lists(profile_id).select_related("profile"))
    if not pin_lists or not pin_ids:
        return
    among = Pin.objects.filter(profile_id=profile_id, pk__in=list(pin_ids))
    live_ids = sorted(among.values_list("pk", flat=True))
    if not live_ids:
        return
    among = Pin.objects.filter(pk__in=live_ids)
    memberships = {(item.pin_list_id, item.pin_id): item for item in PinListItem.objects.filter(pin_list__in=pin_lists, pin_id__in=live_ids).only("pk", "pin_list_id", "pin_id", "added_via")}

    for pin_list in pin_lists:
        by_filter = filter_matching_ids(pin_list, among=among)
        by_boundary = _boundary_matching_ids(pin_list, among=among)
        to_add: list[tuple[int, str]] = []
        to_remove: list[int] = []
        for pin_id in live_ids:
            rule = PinListItem.ADDED_SMART_FILTER if pin_id in by_filter else PinListItem.ADDED_BOUNDARY if pin_id in by_boundary else None
            existing = memberships.get((pin_list.pk, pin_id))
            if rule is not None and existing is None:
                to_add.append((pin_id, rule))
            elif rule is None and existing is not None and existing.added_via != PinListItem.ADDED_MANUAL:
                to_remove.append(existing.pk)
            # matches + present, or not-matches + manual: no-op either way - manual always wins.
        if to_remove:
            PinListItem.objects.filter(pk__in=to_remove).delete()
        if to_add:
            base_order = pin_list.items.count()
            # ignore_conflicts: a manual add or a full resync can insert the same row meanwhile, and either one is right.
            PinListItem.objects.bulk_create(
                [PinListItem(pin_list=pin_list, pin_id=pin_id, order=base_order + i, added_via=rule) for i, (pin_id, rule) in enumerate(to_add)],
                ignore_conflicts=True,
            )


def resync_smart_list(pin_list: PinList, *, filter_ids: set[int] | None = None) -> None:
    """Fully recompute one list's membership against its current smart_filter/smart_boundary rules.
    ``is_smart`` only gates whether *future* pin edits keep re-triggering this (see ``services.pins.smart_list_sync``, fed by Pin's post_save/labels-m2m signals).

    Args:
        pin_list: The list whose ``smart_filter``/``smart_boundary`` just changed.
        filter_ids: Precomputed result of ``filter_matching_ids(pin_list)``, for callers that already resolved it (e.g. resyncing several lists that share the exact same freshly-saved ``smart_filter`` criteria) and want to skip re-resolving the same..."""
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

    # ignore_conflicts: a queued sync (services.pins.smart_list_sync) can add one of these rows after `current` was
    # read, and either insert is right.
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
        ignore_conflicts=True,
    )


def filter_matching_ids(pin_list: PinList, *, among: PinQuerySet | None = None) -> set[int]:
    """Resolve ``pin_list.smart_filter`` into the set of currently-matching pin ids.

    The filter always runs over all of the owner's root pins, and *among* only narrows what it reports: a criterion
    such as ``overlapping_pins`` compares each pin with the rest, so evaluating it over a few pins would change
    the answer. For every other criterion the narrowing is one more condition in the same query.

    Args:
        pin_list: The list whose ``smart_filter`` to resolve.
        among: Only report these pins, rather than all of the list owner's.

    Returns:
        Set of matching pin ids, or an empty set when there's no smart_filter."""
    if not pin_list.smart_filter:
        return set()
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.search.filter_criteria import deserialize_criteria

    criteria = deserialize_criteria(pin_list.smart_filter, pin_list.profile)
    # root_pins(): every saved-filter preview call site (controllers/saved_filters.py) excludes detail/child pins
    # before matching criteria, so a child pin never joins a list its filter's preview would not have shown it on.
    matches = Pin.objects.filter(profile=pin_list.profile_id).root_pins().filter_by_criteria(criteria)
    if among is not None:
        matches = matches.filter(pk__in=among.values("pk"))
    return set(matches.values_list("pk", flat=True))


def add_pins_to_list(pin_list: PinList, pins: Sequence[Pin], *, added_via: str | None = None) -> ListAddResult:
    """Add *pins* to *pin_list*, skipping duplicates and honoring the per-list cap.
    Extracted verbatim from ``controllers.pin_lists.PinListAddPinsView.post`` so the web UI and the external API share one implementation.

    Args:
        pin_list: The list to add to.
        pins: The pins to add.
        added_via: Provenance stamped on the new rows.

    Returns:
        A :class:`ListAddResult` describing what happened."""
    return add_pin_ids_to_list(pin_list, [pin.pk for pin in pins], added_via=added_via)


def add_pin_ids_to_list(pin_list: PinList, pin_ids: Sequence[int], *, added_via: str | None = None) -> ListAddResult:
    """Add the pins with *pin_ids* to *pin_list*, skipping duplicates and honoring the per-list cap.

    Args:
        pin_list: The list to add to.
        pin_ids: Primary keys of pins the caller has already scoped to the list's owner.
        added_via: Provenance stamped on the new rows.

    Returns:
        A :class:`ListAddResult` describing what happened."""
    from urbanlens.dashboard.models.pin_list.model import PinListItem
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    provenance = PinListItem.ADDED_MANUAL if added_via is None else added_via
    max_pins = SiteSettings.get_current().max_pins_per_list

    with transaction.atomic():
        existing_pin_ids = set(pin_list.items.values_list("pin_id", flat=True))
        # Preserves caller order while de-duplicating a payload that names the
        # same pin twice - without this, two rows for one pin would violate
        # uq_pin_list_item inside bulk_create.
        new_ids: list[int] = []
        seen: set[int] = set()
        for pin_id in pin_ids:
            if pin_id in existing_pin_ids or pin_id in seen:
                continue
            seen.add(pin_id)
            new_ids.append(pin_id)

        base_order = len(existing_pin_ids)
        skipped_over_cap = 0
        if max_pins > 0:
            remaining = max(0, max_pins - base_order)
            if len(new_ids) > remaining:
                skipped_over_cap = len(new_ids) - remaining
                new_ids = new_ids[:remaining]

        if new_ids:
            PinListItem.objects.bulk_create(
                [PinListItem(pin_list=pin_list, pin_id=pin_id, order=base_order + i, added_via=provenance) for i, pin_id in enumerate(new_ids)],
            )

    return ListAddResult(added=len(new_ids), skipped_over_cap=skipped_over_cap, max_pins=max_pins)


def remove_pins_from_list(pin_list: PinList, pin_ids: Sequence[int]) -> int:
    """Remove the given pins from *pin_list*, whatever their provenance.
    ``PinListItem.Meta. ordering = ["order", "created"]`` tolerates gaps, so closing them would be a full-table rewrite for no visible difference.

    Args:
        pin_list: The list to remove from.
        pin_ids: Primary keys of the pins to remove.

    Returns:
        How many membership rows were actually deleted."""
    from urbanlens.dashboard.models.pin_list.model import PinListItem

    deleted, _ = PinListItem.objects.for_list(pin_list).filter(pin_id__in=list(pin_ids)).delete()
    return deleted


def reorder_list_items(pin_list: PinList, item_ids: Sequence[int]) -> int:
    """Renumber *pin_list*'s items so they follow the order given in *item_ids*.

    Args:
        pin_list: The list whose items are being reordered.
        item_ids: ``PinListItem`` primary keys in their new display order.

    Returns:
        How many items were actually renumbered."""
    from urbanlens.dashboard.models.pin_list.model import PinListItem

    return PinListItem.objects.for_list(pin_list).number_in_order(item_ids)


def resync_lists_for_saved_filter(saved_filter: SavedFilter) -> int:
    """Refresh every PinList derived from *saved_filter* against its current criteria.
    ``PinList.smart_filter`` is a one-time *copy* of a SavedFilter's criteria, not a live reference, so a list pointed at a filter silently drifts out of sync the moment that filter is edited.

    Args:
        saved_filter: The filter whose ``criteria`` just changed.

    Returns:
        How many derived lists were resynced."""
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


def _boundary_matching_ids(pin_list: PinList, *, among: PinQuerySet | None = None) -> set[int]:
    """The ids of the list owner's pins inside ``pin_list.smart_boundary``.

    Split at the antimeridian for the same reason ``filter_by_criteria``'s ``include_regions`` is: a boundary drawn
    across the date line arrives with unwrapped coordinates, and a planar ``__within`` against it matches nothing on
    the far side.

    Args:
        pin_list: The list whose boundary to test.
        among: Only report these pins, rather than all of the list owner's.

    Returns:
        Set of matching pin ids, or an empty set when there's no boundary.
    """
    if not pin_list.smart_boundary:
        return set()
    from urbanlens.dashboard.models.pin.model import Pin

    matches = Pin.objects.filter(profile=pin_list.profile_id, location__point__within=split_at_antimeridian(pin_list.smart_boundary))
    if among is not None:
        matches = matches.filter(pk__in=among.values("pk"))
    return set(matches.values_list("pk", flat=True))
