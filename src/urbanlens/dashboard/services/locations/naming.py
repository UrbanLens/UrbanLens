"""Extensible place-name resolution for newly created locations."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
import unicodedata

from django.db import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.locations.name_resolution import NameCandidate

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

# Words (including common abbreviations) that mark a name as a street name
# rather than a place name, used by is_address_derived_name. Matching is on
# whole casefolded tokens, so "St" matches but "Station" does not.
_STREET_TYPE_WORDS: frozenset[str] = frozenset(
    {
        "street",
        "road",
        "place",
        "boulevard",
        "avenue",
        "lane",
        "drive",
        "way",
        "court",
        "terrace",
        "highway",
        "pike",
        "route",
        "st",
        "rd",
        "pl",
        "blvd",
        "ave",
        "ln",
        "dr",
        "hwy",
        "ct",
        "ter",
        "rt",
        "rte",
    },
)

_NAME_TOKEN_PATTERN = re.compile(r"[^a-z0-9]+", re.IGNORECASE)

# Everyday punctuation kept as-is in sanitize_name (beyond letters/digits/space).
# Deliberately excludes markup-significant characters (<, >, backtick, braces,
# backslash, pipe, semicolon, ...) and symbols/emoji, which are dropped instead.
_SAFE_NAME_PUNCTUATION: frozenset[str] = frozenset("-'.,&()/:!?\"#")

# Typographic look-alikes folded to a plain-ASCII equivalent before filtering,
# so e.g. a curly apostrophe survives sanitize_name instead of being dropped.
_NAME_CHAR_SUBSTITUTIONS: dict[str, str] = {
    "‘": "'",  # left single quote
    "’": "'",  # right single quote
    "‚": "'",  # single low-9 quote
    "‛": "'",  # single high-reversed-9 quote
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "„": '"',  # double low-9 quote
    "‟": '"',  # double high-reversed-9 quote
    "–": "-",  # en dash
    "—": "-",  # em dash
    "−": "-",  # minus sign
}

_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")


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
    normalized = stripped.casefold()
    # "Unnamed Location in Albany, NY" (Location.display_name's area-suffixed
    # placeholder) is still a placeholder, not a real place name.
    if normalized.startswith("unnamedlocationin"):
        return False
    return normalized not in _MEANINGLESS_NAME_PHRASES


def is_address_derived_name(name: str, location: Location) -> bool:
    """Return True when a candidate name is merely a fragment of the location's address.

    External sources (Google Places especially) sometimes report the street
    name or the city as the place "name" - e.g. "Westwood Northern Blvd" for
    an address on that street, or "Albany" for a location in Albany, NY. Such
    names identify the surroundings, not the place, so they must not become
    the official name. A name is considered address-derived when:

    * it contains a street-type word (street, road, blvd, ...) **and** appears
      within the location's address - so "Westwood Northern Blvd" is rejected,
      but "Kenwood" at "1 Kenwood Road" is kept (the street was named after
      the place); or
    * it matches or appears within the location's city or state name.

    Comparisons use :func:`normalize_name_for_comparison`, so punctuation,
    case, and spacing differences do not affect the verdict.

    Args:
        name: The candidate name to check.
        location: The location whose address components the name is checked against.

    Returns:
        True when the name is address-derived and should not be saved as an
        official name.
    """
    normalized = normalize_name_for_comparison(name)
    if not normalized:
        return False

    for component in (location.city, location.state):
        normalized_component = normalize_name_for_comparison(component)
        if normalized_component and normalized in normalized_component:
            return True

    tokens = {token.casefold() for token in _NAME_TOKEN_PATTERN.split(name) if token}
    if tokens & _STREET_TYPE_WORDS:
        normalized_address = normalize_name_for_comparison(location.address)
        if normalized_address and normalized in normalized_address:
            return True

    return False


def sanitize_name(value: str | None) -> str | None:
    """Sanitize a user-supplied or externally-sourced place/pin/wiki name.

    Names are reused verbatim in several risky contexts - external API query
    strings (Google, Wikipedia, Brave), AI prompts, and page templates - so
    this normalizes to a strict allowlisted character set rather than only
    blocking a few known-bad characters. Unicode letters and digits from any
    script (accents, CJK, Cyrillic, Arabic, ...) are kept as-is so non-English
    names are unaffected; curly quotes/dashes are folded to their plain-ASCII
    equivalents; a small allowlist of everyday name punctuation is kept; and
    everything else - markup-significant characters, control/formatting
    characters, emoji, other symbols - is dropped.

    This is invoked from the ``save()`` of every model with a user-facing name
    field (Pin, Wiki, Location, alias rows), so it applies regardless of write
    path (HTMX controllers, REST serializer, bulk edit, import, Django admin).
    Length limits are enforced separately by each field's ``max_length``.

    Args:
        value: Raw name text, or ``None``.

    Returns:
        The sanitized name, or the input unchanged if it was falsy.
    """
    if not value:
        return value

    normalized = unicodedata.normalize("NFKC", value)
    for bad, good in _NAME_CHAR_SUBSTITUTIONS.items():
        normalized = normalized.replace(bad, good)

    kept: list[str] = []
    for char in normalized:
        if char.isspace():
            kept.append(" ")
        elif char in _SAFE_NAME_PUNCTUATION or unicodedata.category(char)[0] in ("L", "N"):
            kept.append(char)
        # else: drop the character entirely - a symbol, emoji, or control/
        # formatting character has no place in a name and is a known vector
        # for markup injection or invisible/homograph spoofing.

    return _WHITESPACE_RUN_PATTERN.sub(" ", "".join(kept)).strip()


def _clean_candidate(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if is_meaningful_name(value) else None


def external_name_candidates_for_location(
    location: Location,
    extra_candidates: list[tuple[str, Any]] | None = None,
) -> list[NameCandidate]:
    """Gather cleaned, quality-gated external name candidates for a location.

    Explicit ``extra_candidates`` come first, then every enabled plugin's
    :class:`~urbanlens.dashboard.services.locations.name_resolution.NameProvider`
    contributions in plugin ``(order, name)`` order. Raw values are cleaned
    (:func:`is_meaningful_name`) and address-derived fragments are rejected
    (:func:`is_address_derived_name`); duplicates of the same normalized name
    from the same source are dropped, preserving first-seen order.

    Args:
        location: The location to gather candidates for.
        extra_candidates: Optional ``(source, raw_value)`` pairs to consider
            ahead of plugin-provided candidates (e.g. freshly fetched data not
            yet visible in the cache).

    Returns:
        Cleaned candidates in arrival order.
    """
    from urbanlens.dashboard.plugins.registry import plugin_registry
    from urbanlens.dashboard.services.locations.name_resolution import NameCandidate

    raw: list[tuple[str, Any]] = list(extra_candidates or [])
    for provider in plugin_registry.name_providers():
        try:
            raw.extend((provider.source, value) for value in provider.candidates(location))
        except Exception:
            logger.exception(
                "Name provider '%s' failed for location %s",
                provider.source,
                getattr(location, "pk", None),
            )

    candidates: list[NameCandidate] = []
    seen: set[tuple[str, str]] = set()
    for source, value in raw:
        name = _clean_candidate(value)
        if not name or is_address_derived_name(name, location):
            continue
        key = (source, normalize_name_for_comparison(name))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(NameCandidate(name=name, source=source))
    return candidates


def best_external_name_for_location(
    location: Location,
    extra_candidates: list[tuple[str, Any]] | None = None,
    profile: Profile | None = None,
) -> tuple[str, str] | None:
    """Choose the best externally supplied name for a location.

    Candidates come from plugin name providers (plus any explicit extras) and
    the winner is picked by the configured
    :class:`~urbanlens.dashboard.services.locations.name_resolution.NameResolver`
    - by default, two-source agreement first, then the source priority order
    (the acting profile's personal override when set, else the site-admin default).

    Args:
        location: The location to name.
        extra_candidates: Optional ``(source, raw_value)`` pairs considered
            ahead of plugin candidates.
        profile: The profile whose action triggered this resolution, if any -
            see :func:`default_name_resolver`.

    Returns:
        ``(name, source)`` for the winning candidate, or None when no
        acceptable candidate exists.
    """
    from urbanlens.dashboard.services.locations.name_resolution import default_name_resolver

    candidates = external_name_candidates_for_location(location, extra_candidates=extra_candidates)
    resolved = default_name_resolver(profile).resolve(candidates, location)
    if resolved is None:
        return None
    return resolved.name, resolved.source


def _add_wiki_aliases(wiki, candidates: Sequence[NameCandidate]) -> bool:
    """Persist external name candidates as official WikiAlias rows.

    Every candidate is recorded - including one matching the wiki's current
    name, since the alias list is the full set of known names. Existing rows
    (e.g. user-created aliases with the same name) are left untouched.

    Args:
        wiki: The wiki to attach aliases to; skipped when None or unsaved
            (wikis are created lazily and this honours that).
        candidates: Cleaned candidates to persist.

    Returns:
        True when at least one alias row was created.
    """
    if wiki is None or not getattr(wiki, "pk", None):
        return False
    from urbanlens.dashboard.models.aliases.model import AliasType, WikiAlias

    changed = False
    for candidate in candidates:
        try:
            _alias, created = WikiAlias.objects.get_or_create(
                wiki=wiki,
                name=candidate.name,
                defaults={"kind": AliasType.OFFICIAL, "source": candidate.source},
            )
        except IntegrityError:
            created = False
        changed = changed or created
    return changed


def _add_pin_aliases(location: Location, candidates: Sequence[NameCandidate]) -> bool:
    """Persist external name candidates as official PinAlias rows on every pin at this location.

    Mirrors ``_add_wiki_aliases`` - see its docstring for the "record every
    candidate, leave existing rows alone" reasoning. A Location can have
    several Pins (one per user who's pinned it), so this attaches the same
    candidate set to each of them independently.

    Args:
        location: The location whose pins should receive aliases; skipped
            when unsaved (no pins can exist yet).
        candidates: Cleaned candidates to persist.

    Returns:
        True when at least one alias row was created.
    """
    if location is None or not getattr(location, "pk", None):
        return False
    from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias

    changed = False
    for pin in location.pins.all():
        for candidate in candidates:
            try:
                _alias, created = PinAlias.objects.get_or_create(
                    pin=pin,
                    name=candidate.name,
                    defaults={"kind": AliasType.OFFICIAL, "source": candidate.source},
                )
            except IntegrityError:
                created = False
            changed = changed or created
    return changed


def persist_official_aliases_for_location(location: Location) -> bool:
    """Backfill official aliases for a location's wiki from cached candidates.

    Used when a wiki comes into existence after external data was already
    cached (wikis are created lazily): reads only already-cached candidates -
    no network calls - and records them as official aliases.

    Args:
        location: The location whose wiki should receive official aliases.

    Returns:
        True when at least one alias row was created.
    """
    from django.core.exceptions import ObjectDoesNotExist

    try:
        wiki = location.wiki
    except ObjectDoesNotExist:
        return False
    return _add_wiki_aliases(wiki, external_name_candidates_for_location(location))


def update_location_name_from_external_sources(
    location: Location,
    *,
    extra_candidates: list[tuple[str, Any]] | None = None,
    save: bool = True,
    profile: Profile | None = None,
) -> bool:
    """Refresh a Location's official_name (and its wiki's/pins' names/aliases) from external sources.

    The place-identity name lives on ``Location.official_name``; the
    community-editable name and alias list live on the linked ``Wiki`` (updated
    only when one already exists, honouring lazy wiki creation). All surviving
    candidates are persisted as official aliases - on the wiki AND on every
    pin at this location - *before* any name is written, so the Pin/Wiki
    ``save()`` alias invariant finds correctly attributed rows instead of
    creating user-attributed ones.

    Args:
        location: The location to refresh.
        extra_candidates: Optional ``(source, raw_value)`` pairs considered
            ahead of plugin candidates.
        save: Whether to persist the changes; False computes without writing.
        profile: The profile whose action triggered this refresh, if any - see
            :func:`~urbanlens.dashboard.services.locations.name_resolution.default_name_resolver`.

    Returns:
        True when the location name, wiki name, or alias list changed.
    """
    from django.core.exceptions import ObjectDoesNotExist

    from urbanlens.dashboard.services.locations.name_resolution import default_name_resolver

    candidates = external_name_candidates_for_location(location, extra_candidates=extra_candidates)
    try:
        wiki = location.wiki
    except ObjectDoesNotExist:
        wiki = None

    aliases_changed = _add_wiki_aliases(wiki, candidates)
    aliases_changed = _add_pin_aliases(location, candidates) or aliases_changed

    resolved = default_name_resolver(profile).resolve(candidates, location)
    changed_fields: set[str] = set()
    wiki_changed = False
    if resolved is not None:
        name = resolved.name
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

    return bool(changed_fields) or wiki_changed or aliases_changed
