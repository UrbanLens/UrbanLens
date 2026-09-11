"""A value that changes whenever a profile's map would draw differently.

`Max(Pin.updated)` alone cannot see a deletion - remove any pin but the most
recently updated one and it is unchanged - so a pin deleted in another tab stayed
on the map. Pairing it with the row count closes that.

Everything that changes a pin's appearance without writing the pin row moves
`Pin.updated` through `services.map_pins.touch`, which is why this does not also
aggregate over the label tables. A write path that changes what a pin draws
without touching it would go unnoticed here; fix that in `touch`.
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
