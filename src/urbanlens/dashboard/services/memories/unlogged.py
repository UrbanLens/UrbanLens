"""Surface pins the user marked visited but never logged a dated PinVisit for."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import F

from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Cap the band so a large, long-neglected account doesn't render hundreds of
# cards; mirrors the Photos page's needs-attention limit.
UNLOGGED_VISIT_LIMIT = 60


def unlogged_visited_pins(profile: Profile, *, limit: int = UNLOGGED_VISIT_LIMIT) -> list[Pin]:
    """Return the profile's visited-but-unlogged pins, most-recently-visited first.

    Args:
        profile: The owner whose pins to inspect.
        limit: Maximum number of pins to return.

    Returns:
        Up to ``limit`` top-level pins that are marked visited but have no ``PinVisit`` record, ordered by ``last_visited`` (most recent first, nulls last), then by id for a stable tail order."""
    return list(
        Pin.objects.filter(profile=profile).visited_without_record().select_related("location").order_by(F("last_visited").desc(nulls_last=True), "id")[:limit],
    )
