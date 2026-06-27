"""Extensible place-name resolution for newly created locations."""

from __future__ import annotations

import logging
import re

from urbanlens.dashboard.services.google.places import GooglePlacesGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

# Names that carry no search value when sent to external APIs (Google, Brave, AI, etc.).
# Matching is case-insensitive after stripping whitespace.
_MEANINGLESS_NAME_PHRASES: frozenset[str] = frozenset(
    {
        "",
        "abandoned",
        "abandoned location",
        "abandoned place",
        "coordinate",
        "coordinates",
        "dropped location",
        "dropped pin",
        "gps coordinates",
        "gps location",
        "lat lng",
        "lat/long",
        "location",
        "map marker",
        "map pin",
        "marker",
        "n/a",
        "na",
        "nil",
        "no data",
        "no details",
        "no info",
        "no information available",
        "no name",
        "none",
        "not applicable",
        "not available",
        "null",
        "pin",
        "place",
        "point",
        "selected location",
        "unknown",
        "unknown location",
        "unknown place",
        "unnamed",
        "unnamed activity",
        "unnamed location",
        "unnamed place",
        "unnamed road",
        "untitled",
        "untitled location",
        "untitled pin",
        "new location",
        "new pin",
        "new place",
    },
)

_COORDINATE_NAME_PATTERN = re.compile(
    r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*(?:,|\s)\s*"
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*$",
    re.IGNORECASE,
)


def is_meaningful_name(name: str | None) -> bool:
    """Return True when a place or pin name is worth including in external queries.

    Filters Google/Maps placeholders, coordinate strings used as titles, and other
    generic labels that would not help search, geocoding, or AI prompts.

    Args:
        name: Raw name from a pin, location, or geocoding resolver.

    Returns:
        False for empty values, known placeholder phrases, or coordinate strings;
        True otherwise.
    """
    if not name:
        return False
    if not (stripped := name.strip()):
        return False
    if stripped.casefold() in _MEANINGLESS_NAME_PHRASES:
        return False
    return _COORDINATE_NAME_PATTERN.match(stripped) is None
