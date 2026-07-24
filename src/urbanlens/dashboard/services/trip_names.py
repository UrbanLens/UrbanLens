"""Trip name generation (UL-360): varied suggestions, and defaults for unnamed trips.

The create-trip dialog used to hardcode one placeholder suggestion and require
a name. Now the name is optional - a blank submission gets a generated name -
and the dialog draws its placeholder from this same pool so suggestions don't
go stale.
"""

from __future__ import annotations

import random

#: Flavor + outing pairs. Deliberately generic (no real place names): these
#: name group outings, and must never hint at any user's actual pins.
_FLAVORS = (
    "Rust Belt",
    "Backroads",
    "Off-Season",
    "First Light",
    "Golden Hour",
    "Overgrown",
    "Boiler Room",
    "Freight Line",
    "Watchtower",
    "Powerhouse",
    "Asylum Row",
    "Smokestack",
    "Turbine Hall",
    "Switchyard",
    "Millpond",
    "Brickyard",
    "Undergrowth",
    "Fog Line",
    "Night Shift",
    "Last Call",
)
_OUTINGS = (
    "Run",
    "Ramble",
    "Loop",
    "Expedition",
    "Recon",
    "Crawl",
    "Circuit",
    "Detour",
    "Weekender",
    "Field Trip",
    "Survey",
    "Wander",
)


def random_trip_name() -> str:
    """Return a generated trip name like ``"Rust Belt Ramble"``."""
    # Not used for cryptographic purposes
    return f"{random.choice(_FLAVORS)} {random.choice(_OUTINGS)}"  # noqa: S311 # nosec: B311


def trip_name_suggestions(count: int = 8) -> list[str]:
    """Return ``count`` distinct generated names for the dialog's placeholder rotation."""
    names: set[str] = set()
    while len(names) < min(count, len(_FLAVORS) * len(_OUTINGS)):
        names.add(random_trip_name())
    return sorted(names)
