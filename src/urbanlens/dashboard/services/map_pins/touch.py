"""Recording that a pin's map appearance changed.
A pin can change what it draws without its own row being written - a label's colour, a label's order, a rating - and `bulk_update` never touches `auto_now` columns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.pin.model import Pin


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
    For the writes that change a single pin's payload without writing the pin row: gaining or losing a label, gaining or losing a rating.

    Args:
        pin_id: The pin that changed.

    Returns:
        1 if the pin exists, 0 otherwise."""
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.filter(pk=pin_id).update(updated=timezone.now())


def touch_pins_for_labels(label_ids: Iterable[int]) -> int:
    """Mark every pin carrying any of these labels.
    Which is to say: after any bulk write to `Label`, since deciding otherwise per site is how three of them ended up wrong.

    Args:
        label_ids: Primary keys of the labels that changed. Empty is a no-op
            rather than an error, because the callers compute it from "which
            rows actually moved".

    Returns:
        How many pins were marked."""
    from urbanlens.dashboard.models.pin.model import Pin

    ids = list(label_ids)
    if not ids:
        return 0

    return touch_pins(Pin.objects.filter(labels__in=ids))


def touch_pins_for_label_customization(profile_id: int, label_id: int) -> int:
    """Mark one profile's pins carrying a label whose per-profile override changed.
    A `LabelCustomization` is scoped to one profile, so unlike a label edit this must not reach anybody else's pins - including for a global label, which every profile can carry.

    Args:
        profile_id: Whose customization changed.
        label_id: Which label it overrides.

    Returns:
        How many pins were marked."""
    from urbanlens.dashboard.models.pin.model import Pin

    return touch_pins(Pin.objects.filter(profile_id=profile_id, labels=label_id))
