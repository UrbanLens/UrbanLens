"""TEMPORARY - repairs pins placed at the wrong coordinates by the old CID lookup.

Until 2026-07-25 the pin importer derived a Google-Maps-CID pin's coordinates by
decoding the S2 cell embedded in its Maps URL, which is wrong roughly a third of
the time (see ``docs/designs/redata-cid-resolution.md``). Imports since then resolve the
CID properly via :mod:`services.apis.locations.cid_resolution`, but every ``Pin``
- and every ``Location`` - created by an earlier import is still sitting wherever
the S2 guess put it.

This module lets a *re-import* repair those pins in place: when an imported
record can be matched to one of the user's own pre-cutoff pins, that pin is moved
onto the Location for its freshly-resolved coordinates, instead of a second,
duplicate pin being created some distance away. Two matching strategies, tried in
order:

1. **CID** - the record's CID already resolves to a ``Location`` and the user has
   a pre-cutoff pin sitting on it. That Location is very likely the bad
   S2-derived one, so its pin is the one to move.
2. **Coordinate-shaped name** - a record whose *name* is itself a coordinate
   (Google exports unnamed dropped pins that way) can't be matched by position,
   since position is exactly what's broken. Match on the name text instead,
   against the user's pin names and their :class:`PinAlias` rows - first
   verbatim, then against the same coordinates written in a different format.

   Note the asymmetry: the *imported* name arrives straight out of the file and
   may be in any format, but the *stored* name has been through
   :func:`services.locations.naming.sanitize_name`, which drops symbol
   characters - so a pin whose name was originally DMS is on record as
   ``4207'24"N 7334'04"W``, with the degree signs gone and no longer parseable.
   Decimal and hemisphere-suffixed forms survive sanitizing intact, so those are
   what the cross-format pass realistically compares against.

Scope is deliberately narrow. Only the importing profile's own pins created
strictly before :data:`LEGACY_COORDINATE_CUTOFF` are ever moved; a pin created on
or after the cutoff was placed by the corrected code and is left alone, as are
other users' pins and the shared ``GooglePlace``/CID rows.

When the corrected coordinates land on a Location the profile already has a
*different* pin for (moving the legacy pin there would collide with
``db_pin_unique_location_per_profile``), this raises a
:class:`~urbanlens.dashboard.models.pin_merge_suggestions.model.PinMergeSuggestion`
instead of moving anything - see :mod:`services.pins.pin_merge_suggestions` -
proposing the user merge the legacy pin into the one that's already correctly
placed, with that correct pin always preselected as the survivor.

The import preview also calls :func:`preview_needs_legacy_repair` so it doesn't
pre-deselect a matching record as "already on your map" - its proximity check
would otherwise compare against the very legacy pin this repair exists to move,
which sits at the same wrong, S2-derived coordinates the preview itself shows.

A separate, broader TEMPORARY flag lives entirely in ``google.maps``
(``_csv_row_iter``'s ``s2_guess`` key, consumed by ``_preview_pins``): any
previewed record whose own cid came out of the imprecise S2-cell URL pattern is
force-selected on its own merits, even when this module finds no specific
legacy pin to match - not every affected row still has one to find. It doesn't
call into this module, but shares the same removal criterion below.

**Remove this module together with its call sites in
``services.apis.locations.google.maps`` (each marked with a matching TEMPORARY
comment) once every user has had the chance to re-import their pins.**
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import math
import re
from typing import TYPE_CHECKING

from django.db import DatabaseError
from django.db.models import Q

from urbanlens.dashboard.models.location import Location
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.services.locations.naming import sanitize_name
from urbanlens.dashboard.services.messaging.dm_location_detection import parse_coordinates

if TYPE_CHECKING:
    from decimal import Decimal

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Pins and Locations created before this moment may have been placed from an
#: S2-decoded guess rather than a real CID lookup. UTC rather than the viewer's
#: zone: the boundary is inherently a day wide either way, and a fixed instant
#: keeps the check reproducible.
LEGACY_COORDINATE_CUTOFF = datetime(2026, 7, 25, tzinfo=UTC)

#: How far apart two parsed coordinate pairs may be and still be considered the
#: same place, in degrees (~11 m). Wide enough to absorb the rounding loss of
#: DMS/DDM -> decimal conversion, and narrow enough that two genuinely
#: different pins never collapse into one.
_COORDINATE_MATCH_TOLERANCE = 1e-4

#: Ceiling on how many of a profile's coordinate-named legacy pins are parsed
#: when looking for the same coordinates written in another format. The verbatim
#: name/alias lookup above it is a plain indexed query and is not capped; this
#: fallback exists only for the rare cross-format case, so it must not turn into
#: an unbounded scan for a user with a very large map.
_MAX_CROSS_FORMAT_CANDIDATES = 500

#: Whole string is nothing but coordinate punctuation, hemisphere/DMS letters and
#: digits, and contains at least one digit. Used purely to narrow the SQL
#: candidate set before parsing - anything it lets through still has to parse as
#: a coordinate *and* land on the target coordinates.
_COORDINATE_NAME_HINT = r"""^[-+0-9.,'"°′″nsewdms()\s]*[0-9][-+0-9.,'"°′″nsewdms()\s]*$"""

#: A pin name that is *entirely* one decimal coordinate pair, e.g. "42.1234,
#: -73.5678" or "(42.12 -73.57)". Anchored to the whole string, so unlike
#: ``dm_location_detection``'s prose-scanning equivalent it does not need the
#: three-decimal-place guard against matching ordinary numbers.
_WHOLE_STRING_DECIMAL_PAIR_RE = re.compile(r"^\(?\s*(-?\d{1,2}\.\d+)\s*(?:,\s*|\s+)(-?\d{1,3}\.\d+)\s*\)?$")


def is_legacy_location(location: Location) -> bool:
    """Whether this Location predates the CID coordinate fix.

    A Location created before the cutoff may have been created from an
    S2-decoded guess, so its coordinates can't be trusted to place a pin during
    a re-import - the CID should be resolved again instead.

    Args:
        location: The Location to test.

    Returns:
        True if the Location was created before :data:`LEGACY_COORDINATE_CUTOFF`.
    """
    created = getattr(location, "created", None)
    return created is not None and created < LEGACY_COORDINATE_CUTOFF


def parse_coordinate_name(name: str | None) -> tuple[float, float] | None:
    """Parse a pin name that is *entirely* a coordinate, in any supported format.

    Google Takeout exports an unnamed dropped pin with its coordinates as the
    name, in whichever format the user's client used - so this accepts decimal
    pairs, hemisphere-suffixed decimals, DMS/DDM and Maps-URL forms (the latter
    three via :func:`dm_location_detection.parse_coordinates`), but only when the
    match covers the whole string. A name that merely *mentions* a coordinate
    ("Bridge near 42.1, -73.5") is not a coordinate name and returns None.

    Args:
        name: The pin name to parse.

    Returns:
        ``(latitude, longitude)`` rounded to six decimal places, or None when the
        name is not wholly a coordinate.
    """
    stripped = (name or "").strip()
    if not stripped:
        return None

    match = _WHOLE_STRING_DECIMAL_PAIR_RE.match(stripped)
    if match:
        latitude, longitude = float(match.group(1)), float(match.group(2))
        if _in_range(latitude, longitude):
            return round(latitude, 6), round(longitude, 6)
        return None

    for found in parse_coordinates(stripped):
        if found.matched_text.strip() == stripped:
            return found.latitude, found.longitude
    return None


def repair_legacy_pin_coordinates(
    *,
    profile: Profile,
    cid: int | None,
    name: str,
    latitude: float | Decimal | None,
    longitude: float | Decimal | None,
) -> Pin | None:
    """Move the user's matching pre-cutoff pin onto its corrected coordinates.

    Args:
        profile: The importing profile - only its own pins are ever considered.
        cid: Google Maps CID carried by the record being imported, if any.
        name: Name carried by the record being imported.
        latitude: Freshly-resolved latitude for the record. Callers must only
            pass coordinates from the corrected pipeline (a real CID resolution,
            a cached CID lookup, or literal coordinates read out of the import
            file) - never an S2-decoded guess, or this would move a pin from one
            wrong place to another.
        longitude: Freshly-resolved longitude for the record.

    Returns:
        The pin that was moved, or None when nothing matched, the match was
        already in the right place, the move could not be made safely, or a
        merge suggestion was raised instead (a collision with an existing pin
        at the corrected location) - in every one of which cases the caller
        should fall back to its normal create-or-merge path.
    """
    correct = _as_valid_coordinates(latitude, longitude)
    if correct is None:
        return None

    candidate = _match_by_cid(profile, cid) or _match_by_coordinate_name(profile, name)
    if candidate is None:
        return None

    try:
        correct_location, _ = Location.objects.get_nearby_or_create(correct[0], correct[1])
    except (DatabaseError, ValueError) as exc:
        logger.warning("Legacy coordinate repair could not resolve a Location for pin %s: %s", candidate.pk, exc)
        return None

    if candidate.location_id == correct_location.pk:
        # Already where it belongs - hand it back to the normal path, which
        # finds this same pin and reports it as an existing one.
        return None

    # Moving onto a Location this profile already has a pin for would violate
    # db_pin_unique_location_per_profile. Leave the legacy pin where it is and
    # suggest merging it into the pin that's already correctly placed instead -
    # merging the two is a user decision, not something an import should do
    # silently. See services.pins.pin_merge_suggestions/services.pins.pin_merge.
    existing_pin = Pin.objects.filter(profile=profile, location=correct_location).exclude(pk=candidate.pk).first()
    if existing_pin is not None:
        from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion, PinMergeSuggestionOrigin

        PinMergeSuggestion.objects.upsert(
            profile=profile,
            pin_a=candidate,
            pin_b=existing_pin,
            origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION,
            reason="A legacy pin's coordinates were corrected onto a location you already have a pin for.",
            # Always the correctly-placed pin for this origin, never the
            # general heuristic - the whole point of this repair is fixing
            # coordinates, and the correct-location pin is definitionally right.
            suggested_survivor=existing_pin,
        )
        logger.info(
            "Legacy coordinate repair suggested merging pin %s into pin %s - profile already has a pin at the corrected location.",
            candidate.pk,
            existing_pin.pk,
        )
        return None

    candidate.location = correct_location
    try:
        candidate.save(update_fields=["location", "updated"])
    except DatabaseError as exc:
        logger.warning("Legacy coordinate repair could not move pin %s: %s", candidate.pk, exc)
        return None

    logger.info("Legacy coordinate repair moved pin %s to location %s.", candidate.pk, correct_location.pk)
    return candidate


def preview_needs_legacy_repair(profile: Profile, *, cid: int | None, name: str) -> bool:
    """Whether a previewed import record would repair one of the profile's own legacy pins.

    Runs the same matching :func:`repair_legacy_pin_coordinates` uses (CID,
    then coordinate-shaped name) without needing the corrected coordinates -
    the import preview only needs to know a legacy pin is sitting there, not
    yet where it belongs. Used to stop the preview's "already on your map"
    proximity check from matching against that legacy pin's still-possibly-
    S2-mis-placed coordinates and pre-deselecting the very record that would
    fix it in the confirm step.

    Args:
        profile: The importing profile - only its own pins are considered.
        cid: CID carried by the previewed record, if any.
        name: Name carried by the previewed record.

    Returns:
        True if a matching pre-cutoff pin exists for this profile.
    """
    return _match_by_cid(profile, cid) is not None or _match_by_coordinate_name(profile, name) is not None


def _in_range(latitude: float, longitude: float) -> bool:
    """Whether a parsed pair is a plausible place (in range, not null island)."""
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return False
    return not (latitude == 0.0 and longitude == 0.0)


def _as_valid_coordinates(latitude: float | Decimal | None, longitude: float | Decimal | None) -> tuple[float, float] | None:
    """Coerce a caller-supplied coordinate pair to finite floats, or None.

    Accepts ``Decimal`` as well as ``float``: a coordinate read back off a
    ``Location`` is a ``Decimal``, while a freshly-resolved one is a ``float``.
    """
    if latitude is None or longitude is None:
        return None
    try:
        lat_f, lon_f = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return None
    return lat_f, lon_f


def _legacy_pins(profile: Profile):
    """The profile's pins that predate the CID coordinate fix."""
    return Pin.objects.filter(profile=profile, created__lt=LEGACY_COORDINATE_CUTOFF)


def _match_by_cid(profile: Profile, cid: int | None) -> Pin | None:
    """Find the profile's legacy pin sitting on the Location this CID resolves to.

    Args:
        profile: The importing profile.
        cid: CID carried by the record being imported.

    Returns:
        The oldest matching pin, or None.
    """
    if not cid:
        return None
    location = Location.objects.by_cid(cid).first()
    if location is None:
        return None
    return _legacy_pins(profile).filter(location=location).order_by("created").first()


def _match_by_coordinate_name(profile: Profile, name: str) -> Pin | None:
    """Find the profile's legacy pin whose name (or an alias) is this coordinate.

    Only applies when the imported name is itself wholly a coordinate - matching
    ordinary place names by text would be far too loose to move a pin on.

    Args:
        profile: The importing profile.
        name: Name carried by the record being imported.

    Returns:
        The oldest matching pin, or None.
    """
    target = parse_coordinate_name(name)
    if target is None:
        return None

    # Both the raw name and its sanitized form: the imported name comes straight
    # out of the file, while stored names have been through sanitize_name (see
    # this module's docstring), so for anything but a plain decimal pair the two
    # spellings differ.
    cleaned = " ".join((name or "").split())
    verbatim_q = Q()
    for spelling in {cleaned, sanitize_name(cleaned)}:
        if spelling:
            verbatim_q |= Q(name__iexact=spelling) | Q(aliases__name__iexact=spelling)
    if verbatim_q:
        verbatim = _legacy_pins(profile).filter(verbatim_q).distinct().order_by("created").first()
        if verbatim is not None:
            return verbatim

    # Same place, written differently ("42.1234, -73.5678" vs 42°07'24"N ...).
    candidates = _legacy_pins(profile).filter(Q(name__iregex=_COORDINATE_NAME_HINT) | Q(aliases__name__iregex=_COORDINATE_NAME_HINT)).distinct().prefetch_related("aliases").order_by("created")[:_MAX_CROSS_FORMAT_CANDIDATES]
    for candidate in candidates:
        names = [candidate.name, *(alias.name for alias in candidate.aliases.all())]
        if any(_same_coordinates(parse_coordinate_name(candidate_name), target) for candidate_name in names):
            return candidate
    return None


def _same_coordinates(parsed: tuple[float, float] | None, target: tuple[float, float]) -> bool:
    """Whether two parsed coordinate pairs name the same place."""
    if parsed is None:
        return False
    return abs(parsed[0] - target[0]) <= _COORDINATE_MATCH_TOLERANCE and abs(parsed[1] - target[1]) <= _COORDINATE_MATCH_TOLERANCE
