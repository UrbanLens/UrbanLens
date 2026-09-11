"""A value that changes whenever a profile's map would draw differently.

The client refetches its pins when this changes, so it has to move for every
create, edit and delete. `Max(Pin.updated)` alone does not: deleting any pin
other than the most recently updated one leaves the maximum exactly where it
was, so a pin deleted in another tab stays on the map until the browser's own
cache expires. Pairing the maximum with the row count closes that, because a
delete either removes the maximum or lowers the count, and a delete paired with
a create raises the maximum.

Everything that changes a pin's appearance without writing the pin row -
a label's colour, a label's order, a per-profile override - moves `Pin.updated`
through `services.map_pins.touch`, which is why this does not also need to
aggregate over the label tables. That is a real coupling: if a write path is
ever added that changes what a pin draws *without* touching it, this will not
notice, and the place to fix that is `touch`, not here.

One aggregate, on indexed columns. It is computed on every poll of
`map.pins.meta`, so its cost is paid per user per interval and nothing more
expensive belongs in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Count, Max

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True)
class PinCollectionState:
    """What a profile's root pins look like in aggregate, at one moment."""

    #: The most recent `Pin.updated` among them, or None when there are none.
    last_updated: datetime.datetime | None
    #: How many there are.
    total: int

    @property
    def fingerprint(self) -> str:
        """A short opaque value that changes on any create, edit or delete.

        Returns:
            The value clients compare. Only equality is meaningful - nothing
            should parse it, and its format is free to change.
        """
        stamp = self.last_updated.isoformat() if self.last_updated else "none"
        return f"{stamp}:{self.total}"


def pin_collection_state(profile: Profile) -> PinCollectionState:
    """Aggregate a profile's root pins in one query.

    Args:
        profile: Whose pins to describe.

    Returns:
        The collection's state, from which a fingerprint can be taken.
    """
    from urbanlens.dashboard.models.pin import Pin

    result = Pin.objects.filter(profile=profile).root_pins().aggregate(last_updated=Max("updated"), total=Count("pk"))
    return PinCollectionState(last_updated=result["last_updated"], total=result["total"])
