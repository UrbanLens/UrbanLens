"""Recording that a pin's map appearance changed.

A pin can start drawing differently without the pin row being written at all:
its label's colour changed, or the order of its labels changed and a different
one now supplies the icon (`_winning_display_label` sorts by `-order`). Two
things have to follow, and they are easy to do one of.

**Drop the server's cached copy.** Every bulk label write already did this, each
with its own comment explaining that `bulk_update` fires no `post_save`.

**Move `Pin.updated`.** The browser never reads the server's cache. It polls
`map.pins.meta`, which is `Max(Pin.updated)` over the profile's root pins, and
refetches only when that moves. `bulk_update` does not touch `auto_now` columns,
so three of the four label-reorder paths left the client drawing the old icon
until its own cache expired six hours later (P106). The one path that got it
right did so with a hand-written `UPDATE` and a comment - which is what made the
other three easy to miss.

Both live here now, behind one call, so the next write path that forgets is
missing a function call rather than missing a statement nobody knew about.

Four more triggers turned out to have the same omission, found by asking each of
them rather than by reading the code: adding a label to a pin, removing one,
rating a pin, and deleting a rating. All four dropped the server's cached copy
and none moved `Pin.updated`, so a pin could gain a chip or lose its stars and
say nothing about it. They go through `touch_pin` now.

What is still not routed through here is anything that changes a pin's payload
by writing a *related* row this module does not know about. The way to find the
next one is the way these four were found: change the thing, then ask
`map.pins.meta` whether it noticed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


def touch_pins(pins: QuerySet[Pin]) -> int:
    """Mark every pin in a query as having changed, in one statement.

    Args:
        pins: The pins to mark. Filtered, not sliced - this issues an `UPDATE`
            against whatever the query selects.

    Returns:
        How many rows were written.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    # Through a subquery on the primary key rather than updating `pins` directly:
    # the callers' queries join through `labels`, and a join makes `.update()`
    # either illegal or - with `distinct()` - a write repeated per matching row.
    return Pin.objects.filter(pk__in=pins.values("pk")).update(updated=timezone.now())


def touch_pin(pin_id: int) -> int:
    """Mark one pin as having changed.

    For the writes that change a single pin's payload without writing the pin
    row: gaining or losing a label, gaining or losing a rating. Each of those
    already refreshes that pin's cached payload through a receiver in
    `models/pin/signals.py`; this is the half that tells the client.

    Args:
        pin_id: The pin that changed.

    Returns:
        1 if the pin exists, 0 otherwise.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.filter(pk=pin_id).update(updated=timezone.now())


def touch_pins_for_labels(label_ids: Iterable[int]) -> int:
    """Mark every pin carrying any of these labels, and drop its cached payload.

    Call this after any bulk write to `Label` that changes what a pin draws -
    `icon`, `color`, or `order`. Which is to say: after any bulk write to
    `Label`, since deciding otherwise per site is how three of them ended up
    wrong.

    Args:
        label_ids: Primary keys of the labels that changed. Empty is a no-op
            rather than an error, because the callers compute it from "which
            rows actually moved".

    Returns:
        How many pins were marked.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    ids = list(label_ids)
    if not ids:
        return 0

    carrying = Pin.objects.filter(labels__in=ids)
    touched = touch_pins(carrying)
    _drop_cached_pins_of(carrying)
    return touched


def touch_pins_for_label_customization(profile_id: int, label_id: int) -> int:
    """Mark one profile's pins carrying a label whose per-profile override changed.

    A `LabelCustomization` is scoped to one profile, so unlike a label edit this
    must not reach anybody else's pins - including for a global label, which
    every profile can carry.

    Args:
        profile_id: Whose customization changed.
        label_id: Which label it overrides.

    Returns:
        How many pins were marked.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    carrying = Pin.objects.filter(profile_id=profile_id, labels=label_id)
    touched = touch_pins(carrying)
    _drop_cached_pins_of(carrying)
    return touched


def _drop_cached_pins_of(pins: QuerySet[Pin]) -> None:
    """Drop the whole cached pin set of every profile owning a pin in this query.

    Deliberately coarse. Updating each pin's cached payload in place is what made
    one label edit cost tens of thousands of round trips inside the editing
    user's request (P102); dropping the set is one command per profile and the
    next reader rebuilds it from the database. Queued for after the transaction
    commits, so a rolled-back edit does not throw away a cache that is still
    correct.

    Args:
        pins: The pins whose owners' caches should go.
    """
    from django.db import transaction

    from urbanlens.dashboard.services.map_pins import MapPinCache

    # distinct() in the database, not a set() in Python: without it this reads one
    # row per *pin* to learn a handful of profile ids, which is the shape the whole
    # change exists to remove.
    profile_ids = set(pins.exclude(profile_id=None).values_list("profile_id", flat=True).distinct())
    if not profile_ids:
        return

    def drop() -> None:
        try:
            MapPinCache.clear_for_profiles(profile_ids)
        except (RedisError, ConnectionError, OSError, RuntimeError) as error:
            # A cache that cannot be dropped is stale, not broken: entries carry
            # a TTL, and the client is told to refetch by the `updated` bump that
            # has already been committed above. RuntimeError is in the list
            # because the test suite's network guard raises it rather than a
            # connection error, and a receiver that dies there fails tests about
            # something else entirely - which is what the rest of
            # `models/pin/signals.py` already catches it for.
            logger.warning("Unable to drop cached map pins for %s profile(s): %s", len(profile_ids), error)

    transaction.on_commit(drop)
