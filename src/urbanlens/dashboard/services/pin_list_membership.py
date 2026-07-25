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
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from django.db import IntegrityError
from django.db.models import Q

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_list.model import PinList


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


def _boundary_matching_ids(pin_list: PinList) -> set[int]:
    if not pin_list.smart_boundary:
        return set()
    from urbanlens.dashboard.models.pin.model import Pin

    return set(
        Pin.objects.filter(profile=pin_list.profile, location__point__within=pin_list.smart_boundary).values_list("pk", flat=True),
    )
