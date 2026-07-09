"""Helpers for the Community privacy toggle (Profile.community_enabled)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def bulk_privatize_pins(profile: Profile) -> int:
    """Force every currently non-private pin owned by profile to private.

    Pin.save() enforces the "private while Community is off" invariant on
    every future save, but that does not retroactively touch pins that were
    already public at the moment Community is turned off - this does, in one
    query, so no pin is left publicly visible just because it wasn't re-saved.

    Args:
        profile: The profile whose pins should be privatized.

    Returns:
        The number of pins updated.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.filter(profile=profile, is_private=False).update(is_private=True)
