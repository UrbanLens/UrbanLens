"""Best-effort name/description extraction from an arbitrary attribute bag.

GeoJSON properties, Shapefile attribute columns, and OSM tags all pose the same
problem: the caller does not control the key names (a county GIS portal names its
"description" column differently than an Overpass export names its tags), so a
single fixed lookup can't cover every producer. This module centralises the
fallback heuristic so all three importers behave consistently.
"""

from __future__ import annotations

from typing import Any

#: Keys checked (case-insensitively, in order) when guessing which attribute holds
#: a human-readable name. Covers Google Takeout, GeoJSON/Overpass, Shapefile column
#: naming conventions (often truncated to 10 characters by the DBF format), and OSM
#: tags. ``place`` is checked before ``title`` because feeds like USGS earthquake
#: GeoJSON put a plain location in ``place`` but decorate ``title`` with extra data
#: (e.g. magnitude).
DEFAULT_NAME_KEYS: tuple[str, ...] = ("name", "place", "title", "label", "site_name", "namealt")

#: Keys checked (case-insensitively, in order) when guessing which attribute holds
#: a description. Checked before falling back to serialising the remaining properties.
DEFAULT_DESC_KEYS: tuple[str, ...] = ("description", "desc", "comment", "notes", "note", "address")


def pick_name_and_description(
    properties: dict[str, Any],
    *,
    name_keys: tuple[str, ...] = DEFAULT_NAME_KEYS,
    desc_keys: tuple[str, ...] = DEFAULT_DESC_KEYS,
    fallback_name: str = "Unnamed",
) -> tuple[str, str]:
    """Guess a pin name and description from an arbitrary attribute mapping.

    Args:
        properties: Attribute bag to inspect (GeoJSON properties, a Shapefile
            attribute row, or flattened OSM tags).
        name_keys: Candidate keys tried, in order, for the name. Matched
            case-insensitively.
        desc_keys: Candidate keys tried, in order, for the description. Matched
            case-insensitively.
        fallback_name: Value returned when no candidate name key has a usable value.

    Returns:
        A ``(name, description)`` tuple. When no description key matches, the
        description is built by joining every remaining property (excluding
        whichever key supplied the name) as ``"key: value"`` pairs.
    """
    lowered = {str(key).lower(): value for key, value in properties.items()}

    name_key = _first_matching_key(lowered, name_keys)
    name = str(lowered[name_key]).strip() if name_key else ""
    if not name:
        name = fallback_name

    desc_key = _first_matching_key(lowered, desc_keys)
    if desc_key:
        description = str(lowered[desc_key]).strip()
    else:
        used_keys = {name_key} if name_key else set()
        description = _serialize_remaining(lowered, exclude=used_keys)

    return name, description


def _first_matching_key(lowered: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    """Return the first key in *candidates* present in *lowered* with a non-empty value."""
    for key in candidates:
        value = lowered.get(key)
        if value is not None and str(value).strip():
            return key
    return None


def _serialize_remaining(lowered: dict[str, Any], *, exclude: set[str]) -> str:
    """Join every non-empty property not in *exclude* as ``"key: value"`` pairs."""
    parts = [f"{key}: {value}" for key, value in lowered.items() if key not in exclude and value is not None and str(value).strip()]
    return "; ".join(parts)
