"""Guess where an unplaceable imported pin belongs, from its name alone.

When a Google Maps CID never resolves, the import records a
:class:`~urbanlens.dashboard.models.pin_import_failures.model.PinImportFailure`
carrying little more than the place's *name*. The user is then asked to supply an
address or coordinates by hand, once per failure - and a single import can
produce hundreds.

A name is often enough to do better than nothing:

- Many exported names are literally addresses, frequently without a city or
  postcode ("123 Main St"). Those are geocodable directly.
- A name that is not an address is still a place name, and OpenStreetMap can
  usually find a well-known one.

Both routes produce a *suggestion*, never a placement: the user confirms it. That
distinction is the whole safety argument here, because a guess that is quietly
applied is how pins end up silently wrong.

**On S2-cell decoding.** A Maps CID URL embeds an S2 cell that decodes to an
approximate position. Until 2026-07-25 the importer *placed pins* that way and it
was "wrong roughly a third of the time" (see
``services.apis.locations.legacy_cid_coordinate_fix``), which needed a dedicated
repair module to undo. Right two times in three is useless for placing a pin
silently and genuinely useful for proposing one the owner confirms, so it is used
here - but only ever to *corroborate*:

- when the cell agrees with a geocoded candidate, two independent signals point
  at the same place and the guess is offered with high confidence;
- when there is no geocoded candidate at all, the cell alone is offered as a
  rough area, clearly labelled as such;
- it never *filters*. Rejecting an OSM match because a cell that is wrong a third
  of the time disagrees would discard good guesses at exactly that rate. An
  explicit ``near`` from a caller that trusts its own hint still filters; the
  cell does not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.geo.distance import haversine_km
from urbanlens.dashboard.services.messaging.dm_location_detection import parse_addresses

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure

logger = logging.getLogger(__name__)

#: OSM place classes worth suggesting. A name that resolves only to a road or a
#: postcode is not a place the user pinned, and suggesting one would cost more
#: attention than it saves.
_USEFUL_OSM_CLASSES = frozenset({"amenity", "building", "historic", "leisure", "man_made", "military", "natural", "place", "tourism", "shop", "landuse", "aeroway", "railway"})

#: Minimum OSM ``importance`` for a *name* match. Address matches skip this: an
#: exact street address is specific by construction, whereas a bare name like
#: "The Mill" matches many faint records and needs to clear a bar.
_MIN_NAME_IMPORTANCE = 0.35

#: How far a candidate may sit from a caller-supplied ``near`` hint before it is
#: rejected. Deliberately loose - the hint is an approximation, so this is a
#: sanity bound, not a precision filter.
_MAX_HINT_KM = 55.0

#: How close a decoded S2 cell must be to a geocoded candidate to count as
#: corroboration. The cell locates a rough area rather than a point, so this asks
#: "same place?", not "same coordinates?".
#:
#: Both bounds are true distances rather than degree deltas. A degree of longitude
#: shrinks with latitude - at 60 deg it is half its equatorial width, at 70 deg a
#: third - so a degree-based box silently tightened the further north the pin was,
#: costing corroboration (and therefore confidence) in exactly the high-latitude
#: places this feature is useful.
_AGREEMENT_KM = 16.0

#: Confidence for a guess backed by both a geocoder match and an agreeing S2
#: cell, versus the geocoder alone. Two independent signals agreeing is the
#: strongest evidence available here.
_CONFIDENCE_ADDRESS_CORROBORATED = 0.95
_CONFIDENCE_ADDRESS = 0.8
_CONFIDENCE_NAME_CORROBORATED = 0.85
_CONFIDENCE_NAME_CAP = 0.75

#: Confidence for the S2 cell on its own - deliberately low. It is right about
#: two times in three, which is worth showing as "roughly here" and not worth
#: presenting as an answer.
_CONFIDENCE_AREA_ONLY = 0.4


@dataclass(frozen=True)
class LocationGuess:
    """A suggested location for an unplaceable import.

    Attributes:
        latitude: Suggested latitude.
        longitude: Suggested longitude.
        display_name: The matched place's full name, for showing the user what
            is being suggested.
        source: ``"address"`` when the pin's name parsed as a street address,
            ``"name"`` when it matched a place name, ``"area"`` when only the S2
            cell was available, and ``"...+area"`` when the cell corroborated a
            geocoded match.
        confidence: 0-1, for ordering and for deciding whether to show it at all.
    """

    latitude: float
    longitude: float
    display_name: str
    source: str
    confidence: float


def s2_hint_for(failure: PinImportFailure) -> tuple[float, float] | None:
    """Approximate position from the S2 cell in the failure's Maps URL.

    Args:
        failure: The unresolved import row.

    Returns:
        An approximate (latitude, longitude), or None when the row carries no
        usable URL. Wrong about one time in three - see the module docstring.
    """
    url = (getattr(failure, "maps_url", "") or "").strip()
    if not url:
        return None
    from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

    try:
        latitude, longitude = GoogleGeocodingGateway().extract_coordinates_from_url(url)
    except Exception:
        logger.exception("s2_hint_for: could not decode a position from the maps url on import failure %s", failure.pk)
        return None
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def _agrees(latitude: float, longitude: float, hint: tuple[float, float] | None) -> bool:
    """Whether a candidate sits in the same area as the decoded S2 cell.

    Args:
        latitude: Candidate latitude.
        longitude: Candidate longitude.
        hint: The decoded cell position, or None.

    Returns:
        True when both are present and within :data:`_AGREEMENT_DEGREES`.
    """
    if hint is None:
        return False
    return haversine_km(latitude, longitude, hint[0], hint[1]) <= _AGREEMENT_KM


def _within_hint(latitude: float, longitude: float, near: tuple[float, float] | None) -> bool:
    """Whether a candidate is close enough to an area hint to be plausible.

    Args:
        latitude: Candidate latitude.
        longitude: Candidate longitude.
        near: Approximate (latitude, longitude) the pin is believed to be near,
            or None when the caller has no hint.

    Returns:
        True when there is no hint, or the candidate is within
        :data:`_MAX_HINT_KM` of it.
    """
    if near is None:
        return True
    return haversine_km(latitude, longitude, near[0], near[1]) <= _MAX_HINT_KM


def _candidate(raw: dict, *, source: str, confidence: float) -> LocationGuess | None:
    """Build a guess from one Nominatim result, or None if it is unusable.

    Args:
        raw: A single Nominatim search result.
        source: ``"address"`` or ``"name"``.
        confidence: Confidence to record on the guess.

    Returns:
        The guess, or None when the result carries no usable coordinates.
    """
    try:
        latitude = float(raw["lat"])
        longitude = float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return LocationGuess(
        latitude=latitude,
        longitude=longitude,
        display_name=str(raw.get("display_name") or "").strip(),
        source=source,
        confidence=confidence,
    )


def guess_for_failure(failure: PinImportFailure, *, near: tuple[float, float] | None = None) -> LocationGuess | None:
    """Suggest where an unplaceable imported pin belongs.

    Combines two independent signals - what the pin's *name* geocodes to, and the
    rough position of the S2 cell in its Maps URL - so that agreement between them
    raises confidence, and neither is trusted alone more than it deserves. See the
    module docstring for why the cell never rejects a geocoded match.

    Args:
        failure: The unresolved import row.
        near: Optional approximate (latitude, longitude) from a caller that
            trusts its own hint. Unlike the S2 cell, this *does* filter:
            candidates further than :data:`_MAX_HINT_DEGREES` away are discarded.

    Returns:
        The best guess, or None when nothing clears the bar. Returning None is the
        normal outcome for a vague name with no URL, and is preferable to a wrong
        pin.
    """
    name = (failure.name or "").strip()
    hint = s2_hint_for(failure)

    if len(name) < 3:
        return _area_only_guess(hint, near)

    from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

    gateway = NominatimGateway()

    # An address in the name is the strong signal: it is specific by
    # construction, so it does not have to clear the importance bar a bare name
    # does. parse_addresses is shared with DM location detection rather than
    # re-derived here, so both agree on what "looks like an address" means.
    for address in parse_addresses(name):
        try:
            results = gateway.search(address, limit=3)
        except RateLimitExceededError:
            # Routine, not exceptional: Nominatim's policy caps us at one call a
            # minute, and the queue reveals a card per scroll. Logging a traceback
            # per refused card would bury the real geocoder failures below in
            # hundreds of expected ones. The S2 area guess still stands in.
            logger.debug("guess_for_failure: geocoder rate limit reached for import failure %s", failure.pk)
            return _area_only_guess(hint, near)
        except Exception:
            logger.exception("guess_for_failure: address lookup failed for import failure %s", failure.pk)
            return _area_only_guess(hint, near)
        for raw in results:
            guess = _candidate(raw, source="address", confidence=_CONFIDENCE_ADDRESS)
            if guess is None or not _within_hint(guess.latitude, guess.longitude, near):
                continue
            if _agrees(guess.latitude, guess.longitude, hint):
                return replace(guess, confidence=_CONFIDENCE_ADDRESS_CORROBORATED, source="address+area")
            return guess

    try:
        results = gateway.search(name, limit=5)
    except RateLimitExceededError:
        logger.debug("guess_for_failure: geocoder rate limit reached for import failure %s", failure.pk)
        return _area_only_guess(hint, near)
    except Exception:
        logger.exception("guess_for_failure: name lookup failed for import failure %s", failure.pk)
        return _area_only_guess(hint, near)

    corroborated: LocationGuess | None = None
    best: LocationGuess | None = None
    for raw in results:
        if str(raw.get("class") or "") not in _USEFUL_OSM_CLASSES:
            continue
        try:
            importance = float(raw.get("importance") or 0.0)
        except (TypeError, ValueError):
            importance = 0.0
        if importance < _MIN_NAME_IMPORTANCE:
            continue
        guess = _candidate(raw, source="name", confidence=min(_CONFIDENCE_NAME_CAP, importance))
        if guess is None or not _within_hint(guess.latitude, guess.longitude, near):
            continue
        # A match the cell agrees with wins over a more "important" one it does
        # not: two signals pointing at the same place beats one pointing harder.
        if corroborated is None and _agrees(guess.latitude, guess.longitude, hint):
            corroborated = replace(guess, confidence=_CONFIDENCE_NAME_CORROBORATED, source="name+area")
        if best is None:
            best = guess

    return corroborated or best or _area_only_guess(hint, near)


def _area_only_guess(hint: tuple[float, float] | None, near: tuple[float, float] | None) -> LocationGuess | None:
    """The decoded cell on its own, when the name yielded nothing.

    Args:
        hint: The decoded S2 position, or None.
        near: A caller-supplied hint to sanity-check against.

    Returns:
        A low-confidence area guess, or None when there is no cell.
    """
    if hint is None or not _within_hint(hint[0], hint[1], near):
        return None
    return LocationGuess(
        latitude=hint[0],
        longitude=hint[1],
        display_name="",
        source="area",
        confidence=_CONFIDENCE_AREA_ONLY,
    )
