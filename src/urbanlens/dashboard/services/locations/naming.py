"""Extensible place-name resolution for newly created locations."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.google.places import GooglePlacesGateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

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
    """Return True when a place or pin name is worth including in external queries."""
    if not name:
        return False
    if not (stripped := name.strip()):
        return False
    if stripped.casefold() in _MEANINGLESS_NAME_PHRASES:
        return False
    return _COORDINATE_NAME_PATTERN.match(stripped) is None


def _clean_candidate(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if is_meaningful_name(value) else None


def best_external_name_for_location(
    location: Location,
    extra_candidates: list[tuple[str, Any]] | None = None,
) -> tuple[str, str] | None:
    """Choose the best externally supplied name for a location.

    Preference order intentionally favours structured place data over broader
    encyclopedia or park data: explicit candidates (usually freshly loaded
    Google Places details), cached Google place names, cached Google Places,
    Wikipedia, then NPS.
    """
    candidates: list[tuple[str, Any]] = []
    candidates.extend(extra_candidates or [])
    if getattr(location, "google_place_id", None) and getattr(location, "google_place", None):
        candidates.append(("google_place", location.google_place.cached_place_name))

    try:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        for service_name, key_path in (
            ("google_places", ("name",)),
            ("wikipedia", ("title",)),
            ("nps", ("fullName", "name")),
        ):
            cached = LocationCache.get_fresh(location, service_name)
            data = cached.data if cached else None
            if isinstance(data, dict):
                for key in key_path:
                    candidates.append((service_name, data.get(key)))
    except Exception:
        logger.debug(
            "Could not inspect external name caches for location %s",
            getattr(location, "pk", None),
            exc_info=True,
        )

    for source, value in candidates:
        if name := _clean_candidate(value):
            return name, source
    return None


def update_location_name_from_external_sources(
    location: Location,
    *,
    extra_candidates: list[tuple[str, Any]] | None = None,
    save: bool = True,
) -> bool:
    """Replace a meaningless Location.name with the best externally loaded name."""
    if is_meaningful_name(location.name):
        return False
    resolved = best_external_name_for_location(location, extra_candidates=extra_candidates)
    if resolved is None:
        return False
    name, _source = resolved
    if location.name == name:
        return False
    location.name = name
    if save and location.pk:
        location.save(update_fields=["name", "updated"])
    return True


def update_pin_name_from_external_sources(
    pin: Pin,
    *,
    extra_candidates: list[tuple[str, Any]] | None = None,
    save: bool = True,
) -> bool:
    """Replace an auto/placeholder pin label unless the user typed a name."""
    if pin.name_is_user_provided or is_meaningful_name(pin.name):
        return False
    if pin.location_id:
        update_location_name_from_external_sources(pin.location, extra_candidates=extra_candidates, save=save)
        return False
    if not extra_candidates:
        return False
    for _source, value in extra_candidates:
        if name := _clean_candidate(value):
            pin.name = name
            if save and pin.pk:
                pin.save(update_fields=["name", "updated"])
            return True
    return False
