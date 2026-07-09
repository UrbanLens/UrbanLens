"""Google Takeout "My Activity" (Maps) importer.

Processes the flat ``MyActivity.html`` file Google Takeout produces for the "My
Activity" > Maps category (distinct from "Timeline"/Semantic Location History,
which is JSON and already handled by ``location_history.py``). Every "Directions
to <place>" entry is treated as evidence the user travelled there: entries whose
destination falls within ``MY_ACTIVITY_MATCH_RADIUS_M`` metres of an existing pin
owned by the target profile get a PinVisit record created directly, exactly like
the Location History importer. Unmatched entries are not discarded and pins are
never auto-created for them (a "Directions to" lookup covers everyday life -
grocery stores, gas stations, a friend's house - not just places worth mapping),
so they are queued as a self-directed VisitSuggestion the user can accept or
reject from their notifications. Other Maps activity types ("Searched for X",
"Viewed area around X") are out of scope and skipped.

Typical usage (called from maps.GoogleMapsGateway.import_pins_streaming):

    from urbanlens.dashboard.services.apis.locations.google.my_activity import (
        looks_like_my_activity,
        import_my_activity_streaming,
    )
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from django.db import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

MY_ACTIVITY_MATCH_RADIUS_M = 100

# Entries are split on this boundary before any other regex runs, so each
# "Directions to" search is scoped to a single entry's bounded HTML rather than
# scanning across however many unrelated entries (Search, YouTube, other Maps
# activity) sit between two real matches in a huge file - without this, a lazy
# `.*?` spanning the whole document would be quadratic on adversarial input.
_ENTRY_SPLIT_RE = re.compile(r'<div class="outer-cell')

_MAPS_HEADER_RE = re.compile(r'<p class="mdl-typography--title">\s*Maps\s*<br\s*/?>\s*</p>', re.IGNORECASE)

_DIRECTIONS_RE = re.compile(
    r'<div class="content-cell[^"]*">\s*Directions to\s*<a\s+href="([^"]*)"[^>]*>(.*?)</a>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)

_COORD_RE = re.compile(r"(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)")
_TAG_RE = re.compile(r"<[^>]+>")
_BR_SPLIT_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

# US timezone abbreviations Google Takeout activity timestamps commonly carry.
# Checked before falling back to dateparser so the common case avoids its cost.
_TZ_OFFSET_HOURS: dict[str, int] = {
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
    "AKST": -9,
    "AKDT": -8,
    "HST": -10,
    "UTC": 0,
    "GMT": 0,
}

_TIMESTAMP_FORMAT = "%b %d, %Y, %I:%M:%S %p"


def looks_like_my_activity(text: str) -> bool:
    """Cheap sniff for a Google Takeout My Activity HTML export.

    Args:
        text: A prefix of the decoded file text - a few KB is enough, the
            marker classes appear near the top of every My Activity export.

    Returns:
        True when the text carries the Material Design Lite class Google's My
        Activity template always emits, alongside a "Maps" activity entry.
    """
    return "mdl-typography--title" in text and "Maps" in text


def _clean_destination_name(name_html: str) -> str:
    """Strip any nested tags from a destination link's inner HTML and unescape entities."""
    return html.unescape(_TAG_RE.sub("", name_html)).strip()


def _extract_coordinates(href: str, tail: str) -> tuple[float, float] | None:
    """Return the destination (latitude, longitude) for a "Directions to" entry.

    Prefers the last plain-text "lat,lng" line in *tail* (unambiguous - a
    multi-stop entry's URL can carry more than one coordinate-shaped segment).
    Falls back to the coordinate embedded in the "dir//" maps *href* when no
    plain-text coordinate line is present.
    """
    coords = _COORD_RE.findall(tail)
    if not coords:
        url_match = _COORD_RE.search(href)
        if url_match is None:
            return None
        coords = [url_match.groups()]

    lat_str, lon_str = coords[-1]
    try:
        # Rounded to match Pin/Location/VisitSuggestion's DecimalField(decimal_places=6) -
        # comparing an un-rounded float against a value that's been through that field
        # (e.g. a re-import's pending-suggestion dedup lookup) would otherwise never match.
        latitude = round(float(lat_str), 6)
        longitude = round(float(lon_str), 6)
    except ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _parse_timestamp(text: str) -> datetime | None:
    """Parse a Google Takeout activity timestamp, e.g. "Jul 3, 2026, 1:18:25 PM EDT".

    Tries a fast strptime plus a hardcoded US timezone-abbreviation lookup
    first (covers the overwhelming majority of real exports), falling back to
    ``dateparser`` - already a project dependency - for anything else.

    Args:
        text: Timestamp text, tags already stripped.

    Returns:
        A tz-aware datetime, or None if the text isn't a recognisable timestamp.
    """
    text = text.strip()
    if not text:
        return None

    body, _, tz_abbr = text.rpartition(" ")
    offset_hours = _TZ_OFFSET_HOURS.get(tz_abbr.upper()) if body else None
    if offset_hours is not None:
        try:
            naive = datetime.strptime(body, _TIMESTAMP_FORMAT)
        except ValueError:
            pass
        else:
            return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours)))

    import dateparser

    parsed = dateparser.parse(text, settings={"RETURN_AS_TIMEZONE_AWARE": True})
    if parsed is None:
        logger.debug("Unparseable My Activity timestamp: %s", text)
    return parsed


def _extract_timestamp(tail: str) -> datetime | None:
    """Return the timestamp from a "Directions to" entry's trailing ``<br>``-separated lines.

    Tries each line from last to first (the timestamp is normally the final
    line, but this tolerates entries missing the origin/coordinate lines).
    """
    lines = [html.unescape(_TAG_RE.sub("", line)).strip() for line in _BR_SPLIT_RE.split(tail)]
    for line in reversed(lines):
        if not line:
            continue
        parsed = _parse_timestamp(line)
        if parsed is not None:
            return parsed
    return None


def parse_my_activity_entries(html_bytes: bytes) -> Generator[dict[str, Any], None, None]:
    """Yield one dict per qualifying "Directions to" Maps entry in a My Activity export.

    Args:
        html_bytes: Raw ``MyActivity.html`` file bytes.

    Yields:
        Dict with keys: ``destination_name`` (str, HTML-unescaped link text),
        ``latitude``, ``longitude`` (float), ``visited_at`` (tz-aware datetime).
    """
    try:
        text = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Could not decode My Activity file as UTF-8")
        return

    for chunk in _ENTRY_SPLIT_RE.split(text):
        if "Maps" not in chunk or "Directions to" not in chunk:
            continue
        if not _MAPS_HEADER_RE.search(chunk):
            continue

        match = _DIRECTIONS_RE.search(chunk)
        if match is None:
            continue
        href, name_html, tail = match.groups()

        destination_name = _clean_destination_name(name_html)
        if not destination_name:
            continue

        coordinates = _extract_coordinates(href, tail)
        if coordinates is None:
            continue
        latitude, longitude = coordinates

        visited_at = _extract_timestamp(tail)
        if visited_at is None:
            continue

        yield {
            "destination_name": destination_name,
            "latitude": latitude,
            "longitude": longitude,
            "visited_at": visited_at,
        }


def import_my_activity_streaming(
    files: list[tuple[str, bytes]],
    profile: Profile,
    radius_m: int = MY_ACTIVITY_MATCH_RADIUS_M,
) -> Iterator[str]:
    r"""Stream SSE events while importing Google Takeout My Activity (Maps).

    Each parsed "Directions to" entry whose destination matches an existing
    pin gets an idempotent PinVisit(source=HISTORY) created directly, mirroring
    ``location_history.import_location_history_streaming``. Entries that match
    no pin are queued as a self-directed VisitSuggestion instead of being
    discarded or auto-creating a pin - see ``services.visits.create_visit_suggestion``.

    SSE event shapes emitted:

    - ``{type: "start",    total, subtype: "my_activity"}``
    - ``{type: "progress", current, total, percent, matched, suggested,
          skipped, subtype: "my_activity"}``
    - ``{type: "complete", total, matched, suggested, skipped,
          subtype: "my_activity"}``
    - ``{type: "error",    message, subtype: "my_activity"}``

    Args:
        files: List of ``(filename, raw_bytes)`` pairs already extracted
               from any archive by the caller.
        profile: The user profile whose pins are used for proximity matching
            and to whom unmatched entries are suggested.
        radius_m: Match radius in metres (default 100 m).

    Yields:
        SSE-formatted strings (``data: {...}\\n\\n``).
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
    from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
    from urbanlens.dashboard.services.visits import create_visit_suggestion, find_nearest_pin, visit_logging_allowed

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    if not visit_logging_allowed(profile):
        yield sse(
            {
                "type": "error",
                "message": "Visit logging is turned off - enable it in Settings to import your activity history.",
                "subtype": "my_activity",
            },
        )
        return

    all_entries: list[dict[str, Any]] = []
    for filename, raw_bytes in files:
        batch = list(parse_my_activity_entries(raw_bytes))
        logger.info("Parsed %d 'Directions to' entries from %s", len(batch), filename)
        all_entries.extend(batch)

    if not all_entries:
        yield sse(
            {
                "type": "error",
                "message": "No 'Directions to' entries found in uploaded files.",
                "subtype": "my_activity",
            },
        )
        return

    total = len(all_entries)
    yield sse({"type": "start", "total": total, "subtype": "my_activity"})

    matched = 0
    suggested = 0
    skipped = 0

    for i, entry in enumerate(all_entries, 1):
        pin = find_nearest_pin(entry["latitude"], entry["longitude"], profile, radius_m)
        if pin is not None:
            already_exists = PinVisit.objects.filter(
                pin=pin,
                visited_at=entry["visited_at"],
                source=VisitSource.HISTORY,
            ).exists()
            if not already_exists:
                try:
                    PinVisit.objects.create(pin=pin, visited_at=entry["visited_at"], source=VisitSource.HISTORY)
                    if not pin.last_visited or entry["visited_at"] > pin.last_visited:
                        pin.last_visited = entry["visited_at"]
                        pin.save(update_fields=["last_visited"])
                    matched += 1
                except DatabaseError as exc:
                    logger.warning("Failed to save visit for pin %s: %s", pin.id, exc)
                    skipped += 1
            else:
                skipped += 1
        else:
            location = Location.objects.get_for_point(entry["latitude"], entry["longitude"])
            # create_visit_suggestion only dedupes against an already-accepted visit -
            # a pending suggestion for the same place+date needs its own check here,
            # or re-uploading the same export before the user responds would raise a
            # duplicate suggestion every time (mirrors _suggest_for_unfiled_photo).
            already_pending = (
                VisitSuggestion.objects.for_profile(profile)
                .pending()
                .for_place(location=location, latitude=entry["latitude"], longitude=entry["longitude"])
                .filter(visited_at__date=entry["visited_at"].date())
                .exists()
            )
            suggestion = (
                None
                if already_pending
                else create_visit_suggestion(
                    suggested_to=profile,
                    suggested_by=None,
                    visited_at=entry["visited_at"],
                    location=location,
                    latitude=entry["latitude"],
                    longitude=entry["longitude"],
                    candidate_profiles=[],
                    from_my_activity=True,
                    destination_label=entry["destination_name"],
                )
            )
            if suggestion is not None:
                suggested += 1
            else:
                skipped += 1

        yield sse(
            {
                "type": "progress",
                "current": i,
                "total": total,
                "percent": min(100, int(i / total * 100)),
                "matched": matched,
                "suggested": suggested,
                "skipped": skipped,
                "subtype": "my_activity",
            },
        )

    yield sse(
        {
            "type": "complete",
            "total": total,
            "matched": matched,
            "suggested": suggested,
            "skipped": skipped,
            "subtype": "my_activity",
        },
    )
