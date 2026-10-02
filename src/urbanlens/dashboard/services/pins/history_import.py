"""Location History, My Activity and GPS tracks carried from an import preview to its confirmed import.

The preview's sandbox parse fills an :class:`ImportedHistory` from the uploaded bytes. Its JSON form waits
beside the preview until the user confirms, and the confirmed import applies it on a worker without
reading any uploaded file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import logging
from typing import IO, TYPE_CHECKING, Any

from urbanlens.dashboard.services.import_formats.json_stream import iter_array_items, top_level_value_kind
from urbanlens.dashboard.services.import_formats.streams import as_stream

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute

logger = logging.getLogger(__name__)

_VISIT_KEYS = ("latitude", "longitude", "visited_at")
_ACTIVITY_KEYS = ("destination_name", "latitude", "longitude", "visited_at")


@dataclass
class ImportedHistory:
    """What an upload holds besides pins, in a JSON-serialisable form.

    Attributes:
        visits: Location History place visits: ``latitude``, ``longitude``, ISO ``visited_at``.
        activity: My Activity "Directions to" entries: ``destination_name``, ``latitude``,
            ``longitude``, ISO ``visited_at``.
        routes: GPX tracks and routes and Location History trips, as ``ParsedRoute.to_json``.
    """

    visits: list[dict[str, Any]] = field(default_factory=list)
    activity: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Whether there is anything to import."""
        return bool(self.visits or self.activity or self.routes)

    def add_location_history(self, content: bytes | IO[bytes], profile: Profile, filename: str) -> None:
        """Add a Semantic Location History file's place visits and trips, reading one timeline entry at a time.

        Args:
            content: The file, as bytes or a seekable binary file positioned at its start.
            profile: The profile the import is for.
            filename: The uploaded name, kept on each trip's Route.

        Raises:
            ValueError: The file is not JSON, or not UTF-8.
            TypeError: The file is JSON but not a Semantic Location History export.
        """
        from urbanlens.dashboard.services.apis.locations.google.location_history import semantic_route, semantic_visit

        stream = as_stream(content)
        start = stream.tell()
        if top_level_value_kind(stream, "timelineObjects") != "array":
            raise TypeError("Not a Semantic Location History export.")
        stream.seek(start)
        for entry in iter_array_items(stream, "timelineObjects"):
            if (visit := semantic_visit(entry)) is not None:
                self.visits.append(_encoded(visit, _VISIT_KEYS))
            if (route := semantic_route(entry, profile, filename)) is not None:
                self.routes.append(route.to_json())

    def add_my_activity(self, content: bytes | IO[bytes]) -> None:
        """Add a My Activity export's "Directions to" entries.

        Args:
            content: The ``MyActivity.html`` file, as bytes or a seekable binary file positioned at its start.
        """
        from urbanlens.dashboard.services.apis.locations.google.my_activity import parse_my_activity_entries

        self.activity.extend(_encoded(entry, _ACTIVITY_KEYS) for entry in parse_my_activity_entries(content))

    def add_routes(self, parsed_routes: Iterable[ParsedRoute]) -> None:
        """Add parsed routes.

        Args:
            parsed_routes: Unsaved routes, whose profile is not kept.
        """
        self.routes.extend(parsed.to_json() for parsed in parsed_routes)

    def extend(self, other: ImportedHistory) -> None:
        """Add everything *other* holds.

        Args:
            other: History read from another file.
        """
        self.visits.extend(other.visits)
        self.activity.extend(other.activity)
        self.routes.extend(other.routes)

    def counts(self) -> dict[str, int]:
        """How many of each kind there are, for the preview.

        Returns:
            ``visits``, ``activity`` and ``routes``.
        """
        return {"visits": len(self.visits), "activity": len(self.activity), "routes": len(self.routes)}

    def to_json(self) -> dict[str, Any]:
        """The form :meth:`from_json` reads back.

        Returns:
            ``visits``, ``activity`` and ``routes`` lists.
        """
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> ImportedHistory:
        """Read :meth:`to_json`'s output, keeping only the lists it recognises.

        Args:
            data: The stored value, or None.

        Returns:
            The history; empty when *data* holds none.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(**{name: [item for item in data.get(name) or [] if isinstance(item, dict)] for name in ("visits", "activity", "routes")})

    def iter_import_events(self, profile: Profile) -> Iterator[dict[str, Any]]:
        """Log the visits, suggest the unmatched places and save the routes, as each importer's events.

        Args:
            profile: The importing profile.

        Yields:
            The events of each importer that has something to do, each carrying its ``subtype``.
        """
        from urbanlens.dashboard.services.apis.locations.google.location_history import iter_location_history_events
        from urbanlens.dashboard.services.apis.locations.google.my_activity import iter_my_activity_events
        from urbanlens.dashboard.services.apis.locations.route_import import iter_route_import_events

        if self.visits:
            yield from iter_location_history_events(_decoded(self.visits), profile)
        if self.activity:
            yield from iter_my_activity_events(_decoded(self.activity), profile)
        if self.routes:
            yield from iter_route_import_events(_rebuilt(self.routes, profile), profile)


def describe_preview(counts: Mapping[str, int], profile: Profile) -> list[str]:
    """What the preview found besides pins, and what the profile's settings will leave out.

    Args:
        counts: :meth:`ImportedHistory.counts`.
        profile: The importing profile.

    Returns:
        One line per kind found, then a line per kind that will be skipped.
    """
    from urbanlens.dashboard.services.visits.visits import route_import_allowed, visit_logging_allowed

    lines = [
        text
        for count, text in (
            (counts.get("routes", 0), _plural(counts.get("routes", 0), "route or trip", "routes and trips")),
            (counts.get("visits", 0), _plural(counts.get("visits", 0), "place visit from Location History", "place visits from Location History")),
            (counts.get("activity", 0), _plural(counts.get("activity", 0), "destination from My Activity", "destinations from My Activity")),
        )
        if count
    ]
    if counts.get("routes") and not route_import_allowed(profile):
        lines.append("Route tracking is off in your settings, so routes will be skipped.")
    if (counts.get("visits") or counts.get("activity")) and not visit_logging_allowed(profile):
        lines.append("Visit logging is off in your settings, so visits will be skipped.")
    return lines


def describe_outcome(results: Mapping[str, Mapping[str, Any]]) -> str:
    """Summarise what the history importers did.

    Args:
        results: Each importer's ``complete`` event, by ``subtype``.

    Returns:
        A short sentence, or ``""`` when nothing was imported.
    """
    routes = results.get("route", {})
    visits = results.get("location_history", {}).get("matched", 0) + results.get("my_activity", {}).get("matched", 0)
    suggested = results.get("my_activity", {}).get("suggested", 0)
    parts = [
        text
        for count, text in (
            (routes.get("created", 0), _plural(routes.get("created", 0), "route saved", "routes saved")),
            (visits, _plural(visits, "visit logged", "visits logged")),
            (suggested, _plural(suggested, "visit suggestion", "visit suggestions")),
        )
        if count
    ]
    if routes.get("reason") == "routes_disabled":
        parts.append("routes skipped: route tracking is off")
    return " · ".join(parts)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count:,} {singular if count == 1 else plural}"


def _encoded(entry: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: entry[key].isoformat() if isinstance(entry[key], datetime) else entry[key] for key in keys}


def _decoded(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decoded = []
    for entry in entries:
        try:
            decoded.append({**entry, "latitude": float(entry["latitude"]), "longitude": float(entry["longitude"]), "visited_at": datetime.fromisoformat(entry["visited_at"])})
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping a malformed history entry")
    return decoded


def _rebuilt(routes: list[dict[str, Any]], profile: Profile) -> list[ParsedRoute]:
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute

    rebuilt = []
    for data in routes:
        try:
            rebuilt.append(ParsedRoute.from_json(data, profile))
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping a malformed route")
    return rebuilt
