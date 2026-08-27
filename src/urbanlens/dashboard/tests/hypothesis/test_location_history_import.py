"""Google Takeout location-history import: matching, idempotency, and cost.

This importer had no test coverage at all, which matters more than usual because a
Takeout export is a bulk input - tens of thousands of `placeVisit` entries is ordinary -
and the loop originally ran a PostGIS nearest-neighbour query, a duplicate-check query
and an SSE frame *per entry*.

The behavioural contract (match within the radius, never double-import a visit, respect
the visit-logging setting) is asserted here alongside the cost properties, so the
optimisations cannot be undone silently and cannot quietly change what gets imported.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.locations.google.location_history import import_location_history_streaming

LAT, LNG = 42.6526, -73.7562


def _timeline(entries: list[tuple[float, float, str]]) -> tuple[str, bytes]:
    """A Semantic Location History file containing one placeVisit per entry."""
    objects = [
        {
            "placeVisit": {
                "visitConfidence": 100,
                "location": {"latitudeE7": int(lat * 1e7), "longitudeE7": int(lng * 1e7), "name": "Somewhere", "placeId": "abc"},
                "duration": {"startTimestamp": stamp},
            },
        }
        for lat, lng, stamp in entries
    ]
    return ("2026_JANUARY.json", json.dumps({"timelineObjects": objects}).encode("utf-8"))


def _events(stream) -> list[dict]:
    """Parse the SSE strings a run yields back into dicts."""
    return [json.loads(chunk.removeprefix("data: ").strip()) for chunk in stream]


class LocationHistoryImportTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))
        self.profile.track_pin_visits = True
        self.profile.save(update_fields=["track_pin_visits"])
        location = baker.make(Location, latitude=LAT, longitude=LNG, point=Point(LNG, LAT, srid=4326))
        self.pin = baker.make(Pin, profile=self.profile, location=location)

    def _run(self, entries: list[tuple[float, float, str]]) -> list[dict]:
        return _events(import_location_history_streaming([_timeline(entries)], self.profile))

    def test_a_visit_within_the_radius_is_imported(self) -> None:
        self._run([(LAT, LNG, "2026-01-01T10:00:00+00:00")])

        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.HISTORY).count(), 1)

    def test_a_visit_far_away_is_not_imported(self) -> None:
        events = self._run([(0.0, 0.0, "2026-01-01T10:00:00+00:00")])

        self.assertEqual(PinVisit.objects.filter(source=VisitSource.HISTORY).count(), 0)
        self.assertEqual(events[-1]["skipped"], 1)

    def test_reimporting_the_same_export_creates_nothing_new(self) -> None:
        """The duplicate check moved from a per-entry query to a prefetched set;
        idempotency across runs is the property that must survive that."""
        entries = [(LAT, LNG, "2026-01-01T10:00:00+00:00")]
        self._run(entries)
        self._run(entries)

        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.HISTORY).count(), 1)

    def test_a_duplicate_within_one_file_is_imported_once(self) -> None:
        """Previously guaranteed by re-querying the database each iteration. The
        prefetched set has to be updated as rows are created to keep it true."""
        stamp = "2026-01-01T10:00:00+00:00"
        self._run([(LAT, LNG, stamp), (LAT, LNG, stamp)])

        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.HISTORY).count(), 1)

    def test_distinct_timestamps_at_one_place_are_all_imported(self) -> None:
        """Guards the dedup above from over-matching: the memo is keyed on
        coordinates, so repeated coordinates must not collapse distinct visits."""
        self._run([(LAT, LNG, "2026-01-01T10:00:00+00:00"), (LAT, LNG, "2026-01-02T10:00:00+00:00")])

        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.HISTORY).count(), 2)

    def test_repeated_coordinates_cost_one_spatial_query(self) -> None:
        """The expensive part of a Takeout import: an export is mostly the same
        few everyday coordinates, each of which used to cost its own PostGIS query."""
        entries = [(LAT, LNG, f"2026-01-{day:02d}T10:00:00+00:00") for day in range(1, 21)]

        # The importer does `from ...visits.visits import find_nearest_pin` *inside* the
        # function, so the name is rebound from the source module on every call - patching
        # it on `location_history` would never intercept.
        from urbanlens.dashboard.services.visits import visits as visits_service

        with mock.patch.object(visits_service, "find_nearest_pin", wraps=visits_service.find_nearest_pin) as nearest:
            self._run(entries)

        self.assertEqual(nearest.call_count, 1, "one lookup per distinct coordinate, not per entry")
        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.HISTORY).count(), 20)

    def test_progress_frames_are_throttled(self) -> None:
        """A bar can render 100 states; a large export used to push one frame per
        entry, making the stream itself a bottleneck.

        Needs well over 100 entries to show anything - below that every entry
        advances the whole-percent counter, so throttling is a no-op by definition.
        Coordinates are far from any pin so this measures frames, not writes.
        """
        entries = [(0.0, float(i) / 1000.0, "2026-01-01T10:00:00+00:00") for i in range(500)]

        progress = [e for e in self._run(entries) if e["type"] == "progress"]

        self.assertLessEqual(len(progress), 102, "expected roughly one frame per whole percent")
        self.assertLess(len(progress), len(entries))
        self.assertEqual(progress[-1]["percent"], 100)
        self.assertEqual(progress[-1]["current"], len(entries))

    def test_visit_logging_off_imports_nothing(self) -> None:
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])

        events = self._run([(LAT, LNG, "2026-01-01T10:00:00+00:00")])

        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(PinVisit.objects.filter(source=VisitSource.HISTORY).count(), 0)

    def test_last_visited_tracks_the_newest_matched_visit(self) -> None:
        self._run([(LAT, LNG, "2026-01-05T10:00:00+00:00"), (LAT, LNG, "2026-01-01T10:00:00+00:00")])

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, datetime(2026, 1, 5, 10, 0, tzinfo=UTC))
