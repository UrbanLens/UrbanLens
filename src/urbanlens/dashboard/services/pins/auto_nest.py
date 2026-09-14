"""Turning confident building data into child pins without being asked."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.services.locations import site_scope

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


def eligible(pin: Pin) -> bool:
    """Whether a pin may have building children created for it unprompted.

    Args:
        pin: The candidate parent.

    Returns:
        True for a root pin whose owner has the feature on, that has never been swept, was never told "no" via the restructure offer, has no children yet (an existing hierarchy is the user's own arrangement), and is not user-typed as a single structure."""
    from urbanlens.dashboard.models.pin.model import PinType

    if pin.parent_pin_id is not None or pin.buildings_auto_nested_at is not None or pin.restructure_offer_dismissed:
        return False
    if not pin.profile.auto_create_building_pins:
        return False
    if pin.pin_type_is_user_provided and pin.pin_type in (PinType.BUILDING, PinType.ENTRANCE):
        return False
    return not pin.detail_pins.exists()


def auto_nest_pin(pin: Pin) -> int:
    """Create child pins (and child wikis) for this pin's confident buildings.
    A no-op unless the property confidently holds several distinct buildings - an ordinary house stays one pin, and stays unswept so it can nest later if more buildings become known.

    Args:
        pin: The parent pin.

    Returns:
        How many child pins were created."""
    from urbanlens.dashboard.models.pin.model import Pin as PinModel
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import confident_buildings, countable_buildings
    from urbanlens.dashboard.services.pins import pin_restructure

    if not eligible(pin):
        return 0

    confident = confident_buildings(site_scope.parcel_buildings(pin.location) or [])
    if len(countable_buildings(confident)) < site_scope.MULTI_BUILDING_THRESHOLD:
        return 0

    to_create = pin_restructure.unmatched_buildings(confident, list(pin.detail_pins.select_related("location")))
    created = pin_restructure.create_building_pins(pin, to_create)
    try:
        pin_restructure.mirror_buildings_to_wiki(pin, to_create, pin.profile)
    except Exception:
        # The wiki mirror is a bonus on top of the pins, not a reason to lose
        # them - and this runs inside fetch/enrichment paths that must survive.
        logger.exception("auto_nest: wiki mirror failed for pin %s", pin.pk)

    # queryset.update, not save(): this runs from fetch paths where a full-row
    # save would race the user's own edits.
    PinModel.objects.filter(pk=pin.pk).update(buildings_auto_nested_at=timezone.now())
    pin.refresh_from_db(fields=["buildings_auto_nested_at"])
    logger.info("auto_nest: created %d building pin(s) under pin %s", created, pin.pk)
    return created


def auto_nest_location(location: Location) -> int:
    """Sweep every eligible root pin standing on a location.

    Args:
        location: The location whose building list just arrived.

    Returns:
        How many child pins were created across all pins."""
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    created = 0
    for pin in PinModel.objects.filter(location=location, parent_pin__isnull=True).select_related("location", "profile"):
        try:
            created += auto_nest_pin(pin)
        except Exception:
            # One user's failure (a constraint collision, say) must not block
            # another's sweep, nor the fetch that triggered it.
            logger.exception("auto_nest: sweep failed for pin %s", pin.pk)
    return created
