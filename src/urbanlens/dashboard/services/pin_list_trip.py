"""Bulk-copy a PinList's pins into a Trip's activities.

Used by both "Create a trip" (new trip) and "Add to trip" (existing trip).
This is always a one-time copy - a list's smart-filter membership changes
never propagate to a trip after the copy runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin_list.model import PinList
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip


def copy_list_pins_to_trip(pin_list: PinList, trip: Trip, added_by: Profile) -> int:
    """Append one TripActivity per pin currently on ``pin_list`` to ``trip``.

    Args:
        pin_list: Source list, copied in its current display order.
        trip: Destination trip; new activities are appended after whatever
            activities it already has.
        added_by: Profile recorded as the activities' creator.

    Returns:
        Number of activities created.
    """
    from urbanlens.dashboard.models.trips.model import TripActivity

    base_order = trip.activities.count()
    items = list(pin_list.items.select_related("pin__location").order_by("order"))
    TripActivity.objects.bulk_create(
        [
            TripActivity(
                trip=trip,
                location=item.pin.location,
                pin=item.pin,
                added_by=added_by,
                order=base_order + i,
                status=TripActivity.STATUS_PROPOSED,
            )
            for i, item in enumerate(items)
        ],
    )
    return len(items)
