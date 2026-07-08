"""Extensible place-name resolution for newly created locations."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError

from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
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
        "abandonedlocation",
        "abandonedplace",
        "coordinate",
        "coordinates",
        "droppedlocation",
        "droppedpin",
        "gpscoordinates",
        "gpslocation",
        "latlng",
        "latlong",
        "location",
        "mapmarker",
        "mappin",
        "marker",
        "na",
        "nil",
        "nodata",
        "nodetails",
        "noinfo",
        "noinformationavailable",
        "noname",
        "none",
        "notapplicable",
        "notavailable",
        "null",
        "pin",
        "place",
        "point",
        "selectedlocation",
        "unknown",
        "unknownlocation",
        "unknownplace",
        "unnamed",
        "unnamedactivity",
        "unnamedlocation",
        "unnamedplace",
        "unnamedroad",
        "untitled",
        "untitledlocation",
        "untitledpin",
        "newlocation",
        "newpin",
        "newplace",
    },
)

_STRIP_NAME_PATTERN = re.compile(r"[^a-z0-9]", re.IGNORECASE)

_DECIMAL_COORDINATE_PATTERN = re.compile(
    r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?[\s,]+"
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*$",
    re.IGNORECASE,
)

# Whitespace around each optional degree/minute/second marker uses possessive
# quantifiers (`\s*+`). A plain `\s*` next to an optional literal that isn't
# in `\s` still lets a run of spaces be split between the two `\s*` groups in
# many equivalent ways once the group in between matches empty - a classic
# polynomial ReDoS. Possessive quantifiers commit to the maximal match and
# never backtrack into it, which removes that ambiguity without changing
# which strings match.
_DMS_COORDINATE_PATTERN = re.compile(
    r"""
    ^\s*+
    \d{1,2}\s*+°?\s*+
    \d{1,2}\s*+['′]?\s*+
    \d+(?:\.\d+)?\s*+(?:"|″)?\s*+[NS]
    \s*+,?\s*+
    \d{1,3}\s*+°?\s*+
    \d{1,2}\s*+['′]?\s*+
    \d+(?:\.\d+)?\s*+(?:"|″)?\s*+[EW]
    \s*+$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Longest plausible decimal/DMS coordinate string is well under this; kept as
# a cheap early-reject before running the regexes, not as a ReDoS mitigation.
_MAX_COORDINATE_NAME_LENGTH = 64


def is_coordinate_name(name: str) -> bool:
    if len(name) > _MAX_COORDINATE_NAME_LENGTH:
        return False
    return _DECIMAL_COORDINATE_PATTERN.match(name) is not None or _DMS_COORDINATE_PATTERN.match(name) is not None


def normalize_name_for_comparison(name: str | None) -> str:
    """Casefold and strip everything but letters/digits, for "is this really the same name" checks.

    Two names that only differ by case, spacing, or punctuation (e.g. "St. Mark's"
    vs "st marks") normalize to the same string, so a straight string comparison
    can be used to catch near-duplicates that would otherwise pass an exact or
    even a case-insensitive equality check.
    """
    if not name:
        return ""
    return _STRIP_NAME_PATTERN.sub("", name).casefold()


def is_meaningful_name(name: str | None) -> bool:
    """Return True when a place or pin name is worth including in external queries."""
    if not name:
        return False
    if not (stripped := _STRIP_NAME_PATTERN.sub("", name)):
        return False
    if is_coordinate_name(name):
        return False
    return stripped.casefold() not in _MEANINGLESS_NAME_PHRASES


def _clean_candidate(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if is_meaningful_name(value) else None


def external_name_candidates_for_location(
    location: Location,
    extra_candidates: list[tuple[str, Any]] | None = None,
) -> list[tuple[str, Any]]:
    """Return raw external name candidates for a location in preference order."""
    candidates: list[tuple[str, Any]] = []
    candidates.extend(extra_candidates or [])
    google_place = location.google_place if getattr(location, "google_place_id", None) else None
    if google_place is not None:
        candidates.append(("google_place", google_place.cached_place_name))

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
    return candidates


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
    for source, value in external_name_candidates_for_location(location, extra_candidates=extra_candidates):
        if name := _clean_candidate(value):
            return name, source
    return None


def _candidate_names(extra_candidates: list[tuple[str, Any]] | None = None) -> list[tuple[str, str]]:
    """Return de-duplicated meaningful external names in source order."""
    candidates: list[tuple[str, Any]] = list(extra_candidates or [])
    names: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, value in candidates:
        if name := _clean_candidate(value):
            key = name.casefold()
            if key not in seen:
                names.append((source, name))
                seen.add(key)
    return names


def _add_wiki_aliases(wiki, names: list[tuple[str, str]]) -> bool:
    """Append external names as WikiAlias rows without duplicating the canonical name."""
    if wiki is None or not getattr(wiki, "pk", None):
        return False
    from urbanlens.dashboard.models.aliases.model import WikiAlias

    canonical = (wiki.name or "").strip().casefold()
    changed = False
    for _source, name in names:
        if name.casefold() == canonical:
            continue
        try:
            _alias, created = WikiAlias.objects.get_or_create(wiki=wiki, name=name)
        except IntegrityError:
            created = False
        changed = changed or created
    return changed


def _add_pin_aliases(pin: Pin, names: list[tuple[str, str]]) -> bool:
    """Append external names as PinAlias rows without duplicating the pin label."""
    if not getattr(pin, "pk", None):
        return False
    from urbanlens.dashboard.models.aliases.model import PinAlias

    canonical_names = {value.strip().casefold() for value in (pin.name, pin.effective_name) if value}
    changed = False
    for _source, name in names:
        if name.casefold() in canonical_names:
            continue
        try:
            _alias, created = PinAlias.objects.get_or_create(pin=pin, name=name)
        except IntegrityError:
            created = False
        changed = changed or created
    return changed


def sync_pin_aliases_after_rename(pin: Pin, previous_name: str) -> None:
    """After a pin's ``name`` has been changed and saved, keep aliases in sync.

    Adds the previous user-given name as a searchable alias (unless it was blank
    or unchanged), and drops any existing alias that now matches the new name so
    the aliases list never contains a redundant near-duplicate of the current name.
    """
    from urbanlens.dashboard.models.aliases.model import PinAlias

    next_name = (pin.name or "").strip()
    previous_name = previous_name.strip()
    if previous_name and previous_name != next_name:
        try:
            PinAlias.objects.get_or_create(pin=pin, name=previous_name)
        except IntegrityError:
            logger.debug("Pin alias already exists for pin %s and name %s", pin.pk, previous_name)

    if next_name:
        normalized_next = normalize_name_for_comparison(next_name)
        for alias in pin.aliases.all():
            if normalize_name_for_comparison(alias.name) == normalized_next:
                alias.delete()


def update_location_name_from_external_sources(
    location: Location,
    *,
    extra_candidates: list[tuple[str, Any]] | None = None,
    save: bool = True,
) -> bool:
    """Refresh a Location's official_name (and its wiki's name/aliases) from external sources.

    The place-identity name lives on ``Location.official_name``; the
    community-editable name and alias list live on the linked ``Wiki`` (updated
    only when one already exists, honouring lazy wiki creation).
    """
    from django.core.exceptions import ObjectDoesNotExist

    resolved = best_external_name_for_location(location, extra_candidates=extra_candidates)
    changed_fields: set[str] = set()
    try:
        wiki = location.wiki
    except ObjectDoesNotExist:
        wiki = None
    wiki_changed = False
    if resolved is not None:
        name, _source = resolved
        if location.official_name != name:
            location.official_name = name
            changed_fields.add("official_name")
        if changed_fields and save and location.pk:
            location.save(update_fields=[*sorted(changed_fields), "updated"])
        # Refresh the community name only when it is not yet meaningful, so a
        # community-edited wiki name is never overwritten.
        if wiki is not None and not is_meaningful_name(wiki.name) and wiki.name != name:
            wiki.name = name
            wiki_changed = True
            if save and wiki.pk:
                wiki.save(update_fields=["name", "updated"])

    alias_names = _candidate_names(external_name_candidates_for_location(location, extra_candidates=extra_candidates))
    aliases_changed = _add_wiki_aliases(wiki, alias_names)
    return bool(changed_fields) or wiki_changed or aliases_changed


def update_pin_name_from_external_sources(
    pin: Pin,
    *,
    extra_candidates: list[tuple[str, Any]] | None = None,
    save: bool = True,
) -> bool:
    """Replace an auto/placeholder pin label unless the user typed a name."""
    name_changed = False
    location = pin.location if pin.location_id else None
    if location is not None:
        name_changed = update_location_name_from_external_sources(location, extra_candidates=extra_candidates, save=save)

    official_candidate = None
    if extra_candidates:
        for _source, value in extra_candidates:
            if name := _clean_candidate(value):
                official_candidate = name
                break
    # official_name is address-derived and lives on Location (refreshed above via
    # update_location_name_from_external_sources); a Pin only carries a personal name.
    if not pin.name_is_user_provided and not is_meaningful_name(pin.name) and official_candidate:
        pin.name = official_candidate
        if save and pin.pk:
            pin.save(update_fields=["name", "updated"])
        name_changed = True

    alias_names = _candidate_names(extra_candidates)
    changed = _add_pin_aliases(pin, alias_names)
    return name_changed or changed
