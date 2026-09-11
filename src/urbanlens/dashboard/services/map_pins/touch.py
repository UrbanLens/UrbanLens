"""Recording that a pin's map appearance changed.

Two things must follow, and it is easy to do only one: drop the server's cached
copy, and move `Pin.updated` so the client's poll of `map.pins.meta` sees it.
Seven write paths did only the first (P106) - a pin can change what it draws
without its own row being written, and `bulk_update` never touches `auto_now`
columns.

Caches are dropped per *profile*, not per pin: rewriting each carrying pin's
payload cost a round trip, two queries and a fresh client each, inside the
editing user's request (P102).
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

    # Subquery on the pk: callers join through `labels`, and `.update()` across a
    # join is either illegal or, with distinct(), repeated per matching row.
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

    # distinct() in SQL, not set() in Python: otherwise this reads a row per pin to
    # learn a handful of profile ids.
    profile_ids = set(pins.exclude(profile_id=None).values_list("profile_id", flat=True).distinct())
    if not profile_ids:
        return

    def drop() -> None:
        try:
            MapPinCache.clear_for_profiles(profile_ids)
        except (RedisError, ConnectionError, OSError, RuntimeError) as error:
            # Stale, not broken: entries carry a TTL and the `updated` bump above is
            # already committed. RuntimeError because the test suite's network guard
            # raises that rather than a connection error.
            logger.warning("Unable to drop cached map pins for %s profile(s): %s", len(profile_ids), error)

    transaction.on_commit(drop)
