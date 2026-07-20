r"""Detect coordinates and street addresses in direct-message text.

Typing a place into a chat is sharing it, so it must count in the sender's
reshare chain exactly like an explicit pin share. When a plaintext message is
sent, its body is scanned for:

- decimal coordinates ("40.7128, -74.0060")
- hemisphere-suffixed decimal degrees ("40.7128 N, 74.0060 W")
- degrees/minutes/seconds ("40°42'46\"N 74°00'22\"W") and degrees + decimal
  minutes ("40°42.767'N 74°00.367'W")
- Google Maps URLs ("…/maps/@40.7128,-74.0060,17z", "…?q=40.7128,-74.0060")
  and ``geo:`` URIs
- street addresses ("123 Main St, Springfield" - forward-geocoded, so this
  path runs async in a Celery task; see ``tasks.detect_dm_address_mentions``)

Each detected place becomes a
:class:`~urbanlens.dashboard.models.direct_messages.location_mention.DirectMessageLocationMention`
(what the chat UI renders under the bubble), and - when the recipient didn't
already have the place pinned - a ``PinShare`` with ``origin=DM_DETECTED``
plus the recipient's ``LocationExposure``. A recipient who already has the
pin gets no share (it wasn't new information), just the mention row so their
own pin's name can be shown back to them (to them only - it's private data).

End-to-end encrypted messages are never scanned - the server has no
plaintext, by design.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, NamedTuple

from django.db import DatabaseError

from urbanlens.dashboard.models.direct_messages.location_mention import DirectMessageLocationMention, LocationMentionKind
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus

if TYPE_CHECKING:
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: Cap on how many distinct places one message can produce, so a pasted wall
#: of coordinates can't mass-create Locations/shares.
MAX_MENTIONS_PER_MESSAGE = 5


class CoordinateMatch(NamedTuple):
    """One coordinate pair found in message text."""

    latitude: float
    longitude: float
    matched_text: str


# Plain "lat, lng" decimal pair. Requires a decimal point in both numbers so
# ordinary prose ("meet at 7, 8 of us") never matches.
_DECIMAL_PAIR_RE = re.compile(r"(?<![\d.-])(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})(?![\d.])")

# "40.7128 N, 74.0060 W" / "40.7128°N 74.0060°W" - hemisphere letters carry the sign.
_DECIMAL_HEMI_RE = re.compile(
    r"(\d{1,2}\.\d+)\s*°?\s*([NS])[,;\s]+(\d{1,3}\.\d+)\s*°?\s*([EW])",
    re.IGNORECASE,
)

# DMS ("40°42'46\"N") and degrees-decimal-minutes ("40°42.767'N") in one
# pattern - seconds are optional, minutes may carry decimals.
_DMS_RE = re.compile(
    r"(\d{1,3})\s*[°d]\s*(\d{1,2}(?:\.\d+)?)\s*['′m]?\s*(?:(\d{1,2}(?:\.\d+)?)\s*[\"″s]\s*)?([NS])"
    r"[,;\s]+"
    r"(\d{1,3})\s*[°d]\s*(\d{1,2}(?:\.\d+)?)\s*['′m]?\s*(?:(\d{1,2}(?:\.\d+)?)\s*[\"″s]\s*)?([EW])",
    re.IGNORECASE,
)

# Google Maps URL forms (`/@lat,lng,17z`, `?q=lat,lng`, `?query=lat,lng`) and geo: URIs.
_MAPS_URL_RE = re.compile(r"(?:[@]|[?&]q(?:uery)?=|geo:)(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)")

# Conservative US-style street address: house number + name + street suffix,
# optionally followed by ", City" / ", City, ST". The suffix list keeps prose
# from matching; geocoding validates whatever slips through.
_STREET_SUFFIXES = "street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way|court|ct|place|pl|circle|cir|highway|hwy|pike|parkway|pkwy|terrace|ter|trail|trl|turnpike|tpke|route|rte"
# Words that never appear between a real house number and street suffix -
# blocks prose like "walked 5 miles down the road" from address-matching.
_ADDRESS_STOPWORDS = "the|a|an|of|to|down|up|along|off|on|for|per|about|around|miles?|mi|km|blocks?|minutes?|hours?|days?"
_ADDRESS_RE = re.compile(
    rf"\b\d{{1,6}}\s+(?:(?!(?:{_ADDRESS_STOPWORDS})\s)[A-Za-z0-9'.-]+\s+){{1,4}}(?:{_STREET_SUFFIXES})\b\.?"
    rf"(?:\s*,\s*[A-Za-z][A-Za-z.'-]*(?: [A-Za-z.'-]+){{0,3}}){{0,2}}(?:\s*,\s*(?-i:[A-Z]{{2}})\b)?(?:\s+\d{{5}}(?:-\d{{4}})?)?",
    re.IGNORECASE,
)


def _valid_coordinates(latitude: float, longitude: float) -> bool:
    """Whether a parsed pair is a plausible place (in range, not null island)."""
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return False
    return not (latitude == 0.0 and longitude == 0.0)


def _dms_to_decimal(degrees: str, minutes: str, seconds: str | None, hemisphere: str) -> float:
    """Convert one DMS/DDM component group to signed decimal degrees."""
    value = float(degrees) + float(minutes) / 60.0 + (float(seconds) / 3600.0 if seconds else 0.0)
    return -value if hemisphere.upper() in {"S", "W"} else value


def parse_coordinates(text: str) -> list[CoordinateMatch]:
    r"""Extract every coordinate pair from free-form text.

    Formats are tried most-specific first (URLs, DMS, hemisphere-suffixed
    decimals, then plain decimal pairs) and overlapping matches are dropped,
    so "40°42'46\"N 74°00'22\"W" never double-matches its embedded numbers.

    Args:
        text: The message body to scan.

    Returns:
        In-order unique matches, capped at :data:`MAX_MENTIONS_PER_MESSAGE`.
    """
    matches: list[tuple[int, int, CoordinateMatch]] = []

    def _claim(start: int, end: int, latitude: float, longitude: float, matched_text: str) -> None:
        if not _valid_coordinates(latitude, longitude):
            return
        for other_start, other_end, _match in matches:
            if start < other_end and end > other_start:
                return
        matches.append((start, end, CoordinateMatch(round(latitude, 6), round(longitude, 6), matched_text.strip())))

    for match in _MAPS_URL_RE.finditer(text):
        _claim(match.start(), match.end(), float(match.group(1)), float(match.group(2)), match.group(0))
    for match in _DMS_RE.finditer(text):
        latitude = _dms_to_decimal(match.group(1), match.group(2), match.group(3), match.group(4))
        longitude = _dms_to_decimal(match.group(5), match.group(6), match.group(7), match.group(8))
        _claim(match.start(), match.end(), latitude, longitude, match.group(0))
    for match in _DECIMAL_HEMI_RE.finditer(text):
        latitude = float(match.group(1)) * (-1.0 if match.group(2).upper() == "S" else 1.0)
        longitude = float(match.group(3)) * (-1.0 if match.group(4).upper() == "W" else 1.0)
        _claim(match.start(), match.end(), latitude, longitude, match.group(0))
    for match in _DECIMAL_PAIR_RE.finditer(text):
        _claim(match.start(), match.end(), float(match.group(1)), float(match.group(2)), match.group(0))

    matches.sort(key=lambda item: item[0])
    seen_pairs: set[tuple[float, float]] = set()
    unique: list[CoordinateMatch] = []
    for _start, _end, match_item in matches:
        pair = (match_item.latitude, match_item.longitude)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        unique.append(match_item)
    return unique[:MAX_MENTIONS_PER_MESSAGE]


def parse_addresses(text: str) -> list[str]:
    """Extract street-address-looking substrings from free-form text.

    Purely lexical - each candidate still has to survive forward geocoding
    (see :func:`detect_address_mentions`) before it produces anything.

    Args:
        text: The message body to scan.

    Returns:
        In-order unique candidates, capped at :data:`MAX_MENTIONS_PER_MESSAGE`.
    """
    seen: set[str] = set()
    results: list[str] = []
    for match in _ADDRESS_RE.finditer(text):
        candidate = match.group(0).strip().rstrip(".,")
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        results.append(candidate)
    return results[:MAX_MENTIONS_PER_MESSAGE]


def _record_mention(message: DirectMessage, location: Location, kind: str, matched_text: str) -> DirectMessageLocationMention | None:
    """Create the mention row (and, when it counts, the DM_DETECTED share) for one place.

    Applies the sharing dedup rule: the recipient already having their own
    pin at the place means this wasn't new information - the mention is still
    stored (to render their pin's name back to them) but no share and no
    exposure are recorded, and it does not count in the sender's chain.

    Args:
        message: The message the place was detected in.
        location: The resolved Location.
        kind: A ``LocationMentionKind`` value.
        matched_text: The exact text that matched, for display.

    Returns:
        The mention row, or None when one already exists for this
        (message, location) or the write failed.
    """
    from urbanlens.dashboard.services.share_provenance import (
        find_profile_pin_near_location,
        profile_is_exposed_to,
        record_share_exposure,
        resolve_and_stamp_origin_share,
        resolve_origin_share,
    )

    try:
        mention, created = DirectMessageLocationMention.objects.get_or_create(
            message=message,
            location=location,
            defaults={"kind": kind, "matched_text": matched_text[:255]},
        )
    except DatabaseError:
        logger.exception("Could not record location mention for message %s", message.pk)
        return None
    if not created:
        return None

    recipient_id = message.recipient_id
    if find_profile_pin_near_location(recipient_id, location) is not None:
        return mention  # Already pinned - reference-only, never a share.

    share = None
    already_shared = PinShare.objects.already_shared_with(recipient_id, location=location).exists() or profile_is_exposed_to(recipient_id, location)
    if not already_shared:
        # The sender's own pin at the place (when they have one) makes the
        # share richer and ties it into their pin's lineage.
        sender_pin = find_profile_pin_near_location(message.sender_id, location)
        parent = resolve_and_stamp_origin_share(sender_pin) if sender_pin is not None else resolve_origin_share(message.sender_id, location=location)
        try:
            share = PinShare.objects.create(
                pin=sender_pin,
                location=location,
                from_profile_id=message.sender_id,
                to_profile_id=recipient_id,
                parent_share=parent,
                origin=PinShareOrigin.DM_DETECTED,
                status=PinShareStatus.PENDING,
                detected_via_message=message,
            )
            record_share_exposure(share)
        except DatabaseError:
            logger.exception("Could not record DM-detected share for message %s", message.pk)
            share = None
    else:
        # The place was already shared with them before; this message still
        # gets the "Add to map" affordance via the earlier acceptable share
        # (pending, or a map/trip-detected one that never materialized a pin).
        share = PinShare.objects.reusable_for(recipient_id, location).first()

    if share is not None:
        mention.pin_share = share
        mention.save(update_fields=["pin_share", "updated"])
    return mention


def detect_coordinate_mentions(message: DirectMessage) -> list[DirectMessageLocationMention]:
    """Scan a message for coordinates and record mentions/shares. Synchronous and DB-only.

    Args:
        message: The just-created message (plaintext; encrypted bodies skip).

    Returns:
        Newly created mention rows (may be empty).
    """
    if message.is_encrypted or not message.body:
        return []
    from urbanlens.dashboard.models.location.model import Location

    mentions = []
    for match in parse_coordinates(message.body):
        try:
            location, _created = Location.objects.get_nearby_or_create(match.latitude, match.longitude)
        except DatabaseError:
            logger.exception("Could not resolve Location for DM coordinates %s", match.matched_text)
            continue
        mention = _record_mention(message, location, LocationMentionKind.COORDINATES, match.matched_text)
        if mention is not None:
            mentions.append(mention)
    return mentions


def _geocode_address(address: str) -> tuple[float, float] | None:
    """Forward-geocode one address candidate to coordinates.

    Uses the Google Geocoding gateway (DB-cached via ``GeocodedLocation``).
    Only street-level results are accepted - a bare city/region match means
    the candidate wasn't really a street address.

    Args:
        address: The candidate address text.

    Returns:
        ``(latitude, longitude)``, or None when unconfigured/no match.
    """
    try:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.google_unrestricted_api_key:
            return None
        data = GoogleGeocodingGateway().geocode_place_name(address)
    except Exception:
        logger.warning("Geocoding failed for DM address candidate %r", address, exc_info=True)
        return None
    results = (data or {}).get("results") or []
    if not results:
        return None
    first = results[0]
    types = set(first.get("types") or [])
    if not types & {"street_address", "premise", "subpremise", "route", "establishment", "point_of_interest"}:
        return None
    geometry = (first.get("geometry") or {}).get("location") or {}
    latitude, longitude = geometry.get("lat"), geometry.get("lng")
    if latitude is None or longitude is None or not _valid_coordinates(float(latitude), float(longitude)):
        return None
    return float(latitude), float(longitude)


def detect_address_mentions(message: DirectMessage) -> list[DirectMessageLocationMention]:
    """Scan a message for street addresses and record mentions/shares.

    Forward-geocodes each candidate, so this belongs in a Celery task
    (``tasks.detect_dm_address_mentions``), never in the request path.

    Args:
        message: The message to scan (plaintext; encrypted bodies skip).

    Returns:
        Newly created mention rows (may be empty).
    """
    if message.is_encrypted or not message.body:
        return []
    from urbanlens.dashboard.models.location.model import Location

    mentions = []
    for candidate in parse_addresses(message.body):
        coordinates = _geocode_address(candidate)
        if coordinates is None:
            continue
        try:
            location, _created = Location.objects.get_nearby_or_create(coordinates[0], coordinates[1])
        except DatabaseError:
            logger.exception("Could not resolve Location for DM address %r", candidate)
            continue
        mention = _record_mention(message, location, LocationMentionKind.ADDRESS, candidate)
        if mention is not None:
            mentions.append(mention)
    return mentions
