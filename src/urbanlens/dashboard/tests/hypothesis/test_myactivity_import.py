"""Tests for services.apis.locations.google.my_activity - Google Takeout My Activity (Maps) import.

Covers:
- parse_my_activity_entries(): extracting "Directions to X" entries from the flat
  HTML Google emits, while skipping other Maps activity types (Searched for X,
  Viewed area around X) and non-Maps entries.
- _parse_timestamp(): the fast US-timezone-abbreviation path and the dateparser fallback.
- import_my_activity_streaming(): matched destinations log a PinVisit directly (mirroring
  the Location History importer); unmatched destinations raise a self-directed
  VisitSuggestion instead of being discarded or auto-creating a pin, with idempotency
  on re-import for both branches.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.locations.google.my_activity import (
    _parse_timestamp,
    import_my_activity_streaming,
    looks_like_my_activity,
    parse_my_activity_entries,
)

# Exact sample block from the real-world MyActivity.html this importer targets.
_DIRECTIONS_ENTRY = (
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Maps<br></p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Directions to '
    '<a href="https://www.google.com/maps/dir//39.2043118,-84.5693664/@39.1634871,-84.6103491,13z/'
    "data=!3m1!4b1!4m4!4m3!1m0!1m1!4e1\">2360 Kipling Ave</a><br>Current location<br>"
    "39.2043118,-84.56936639999999<br>Jul 3, 2026, 1:18:25 PM EDT<br></div>"
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--text-right"></div>'
    '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>'
    "&emsp;Maps<br><b>Locations:</b><br>&emsp;At "
    '<a href="https://www.google.com/maps/@?api=1&amp;map_action=map&amp;center=39.122927,-84.595572&amp;zoom=12">'
    "this general area</a> - Based on your past activity<br><b>Why is this here?</b></div></div></div>"
)

_SEARCHED_FOR_ENTRY = (
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Maps<br></p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Searched for '
    '<a href="https://www.google.com/maps/search/pizza">pizza</a><br>'
    "Jul 2, 2026, 9:00:00 AM EDT<br></div></div></div>"
)

_VIEWED_AREA_ENTRY = (
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Maps<br></p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Viewed area around '
    '<a href="https://www.google.com/maps/@39.0,-84.0,13z">Dayton, OH</a><br>'
    "Jul 1, 2026, 8:00:00 AM EDT<br></div></div></div>"
)

_NON_MAPS_ENTRY = (
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Search<br></p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Directions to '
    '<a href="https://example.com">should not match, wrong header</a><br>'
    "1.0,2.0<br>Jul 1, 2026, 8:00:00 AM EDT<br></div></div></div>"
)


def _wrap_html(*entries: str) -> bytes:
    body = "".join(entries)
    return f"<!DOCTYPE html><html><head><title>My Activity</title></head><body>{body}</body></html>".encode()


class LooksLikeMyActivityTests(SimpleTestCase):
    """looks_like_my_activity() sniffs the Material Design Lite markers My Activity always emits."""

    def test_real_sample_detected(self):
        self.assertTrue(looks_like_my_activity(_wrap_html(_DIRECTIONS_ENTRY).decode()))

    def test_generic_html_not_detected(self):
        self.assertFalse(looks_like_my_activity("<html><body><h1>Hello</h1></body></html>"))

    def test_maps_without_mdl_class_not_detected(self):
        self.assertFalse(looks_like_my_activity("<html><body>Maps are fun</body></html>"))


class ParseMyActivityEntriesTests(SimpleTestCase):
    """parse_my_activity_entries() extracts qualifying 'Directions to' entries only."""

    def test_directions_entry_parses_name_coords_and_timestamp(self):
        entries = list(parse_my_activity_entries(_wrap_html(_DIRECTIONS_ENTRY)))

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["destination_name"], "2360 Kipling Ave")
        self.assertAlmostEqual(entry["latitude"], 39.2043118, places=6)
        self.assertAlmostEqual(entry["longitude"], -84.5693664, places=6)
        self.assertEqual(
            entry["visited_at"],
            datetime.datetime(2026, 7, 3, 13, 18, 25, tzinfo=datetime.timezone(datetime.timedelta(hours=-4))),
        )

    def test_searched_for_entry_skipped(self):
        entries = list(parse_my_activity_entries(_wrap_html(_SEARCHED_FOR_ENTRY)))
        self.assertEqual(entries, [])

    def test_viewed_area_entry_skipped(self):
        entries = list(parse_my_activity_entries(_wrap_html(_VIEWED_AREA_ENTRY)))
        self.assertEqual(entries, [])

    def test_non_maps_header_skipped(self):
        entries = list(parse_my_activity_entries(_wrap_html(_NON_MAPS_ENTRY)))
        self.assertEqual(entries, [])

    def test_mixed_file_extracts_only_directions_entries(self):
        data = _wrap_html(_SEARCHED_FOR_ENTRY, _DIRECTIONS_ENTRY, _VIEWED_AREA_ENTRY, _NON_MAPS_ENTRY)

        entries = list(parse_my_activity_entries(data))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["destination_name"], "2360 Kipling Ave")

    def test_many_non_matching_entries_before_a_match_still_found(self):
        # Guards against quadratic-scan regressions: hundreds of non-Maps/non-Directions
        # entries between the start of the file and the one real match.
        filler = _SEARCHED_FOR_ENTRY * 500
        data = _wrap_html(filler, _DIRECTIONS_ENTRY)

        entries = list(parse_my_activity_entries(data))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["destination_name"], "2360 Kipling Ave")

    def test_html_entities_in_destination_name_unescaped(self):
        entry = (
            '<div class="outer-cell"><div class="mdl-grid">'
            '<div class="header-cell"><p class="mdl-typography--title">Maps<br></p></div>'
            '<div class="content-cell mdl-typography--body-1">Directions to '
            '<a href="https://example.com">Sam&#39;s &amp; Dave&#39;s Diner</a><br>'
            "39.0,-84.0<br>Jul 1, 2026, 8:00:00 AM EDT<br></div></div></div>"
        )

        entries = list(parse_my_activity_entries(_wrap_html(entry)))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["destination_name"], "Sam's & Dave's Diner")

    def test_missing_plain_text_coordinate_falls_back_to_url(self):
        entry = (
            '<div class="outer-cell"><div class="mdl-grid">'
            '<div class="header-cell"><p class="mdl-typography--title">Maps<br></p></div>'
            '<div class="content-cell mdl-typography--body-1">Directions to '
            '<a href="https://www.google.com/maps/dir//10.5,20.5/@10.0,20.0,13z/data=!3m1">Somewhere</a><br>'
            "Jul 1, 2026, 8:00:00 AM EDT<br></div></div></div>"
        )

        entries = list(parse_my_activity_entries(_wrap_html(entry)))

        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0]["latitude"], 10.5, places=6)
        self.assertAlmostEqual(entries[0]["longitude"], 20.5, places=6)

    def test_malformed_entry_does_not_raise(self):
        broken = '<div class="outer-cell"><p class="mdl-typography--title">Maps<br>Directions to <a href="'

        entries = list(parse_my_activity_entries(_wrap_html(broken)))

        self.assertEqual(entries, [])

    def test_non_utf8_bytes_does_not_raise(self):
        entries = list(parse_my_activity_entries(b"\xff\xfe\x00\x01not utf-8"))
        self.assertEqual(entries, [])

    def test_entry_missing_timestamp_skipped(self):
        entry = (
            '<div class="outer-cell"><div class="mdl-grid">'
            '<div class="header-cell"><p class="mdl-typography--title">Maps<br></p></div>'
            '<div class="content-cell mdl-typography--body-1">Directions to '
            '<a href="https://example.com">Nowhere</a><br>39.0,-84.0<br></div></div></div>'
        )

        entries = list(parse_my_activity_entries(_wrap_html(entry)))

        self.assertEqual(entries, [])


class ParseTimestampTests(SimpleTestCase):
    """_parse_timestamp() covers the fast US-abbreviation path and the dateparser fallback."""

    def test_edt_fast_path(self):
        parsed = _parse_timestamp("Jul 3, 2026, 1:18:25 PM EDT")
        self.assertEqual(parsed, datetime.datetime(2026, 7, 3, 13, 18, 25, tzinfo=datetime.timezone(datetime.timedelta(hours=-4))))

    def test_pst_fast_path(self):
        parsed = _parse_timestamp("Jan 15, 2026, 9:05:00 AM PST")
        self.assertEqual(parsed, datetime.datetime(2026, 1, 15, 9, 5, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-8))))

    def test_dateparser_fallback_for_unrecognised_format(self):
        # A format the fast strptime path doesn't match (ISO-style date), exercising
        # the dateparser fallback instead.
        parsed = _parse_timestamp("2026-07-03 13:18:25 EDT")
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.year, parsed.month, parsed.day, parsed.hour), (2026, 7, 3, 13))
        self.assertIsNotNone(parsed.tzinfo)

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_timestamp(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_timestamp("not a timestamp at all !!"))


class ImportMyActivityStreamingTests(TestCase):
    """import_my_activity_streaming() logs visits on matched pins, suggests otherwise."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def _run(self, *entries: str) -> list[dict]:
        import json

        files = [("MyActivity.html", _wrap_html(*entries))]
        events = []
        for line in import_my_activity_streaming(files, self.profile):
            # Each SSE event is "data: {...}\n\n".
            events.append(json.loads(line.removeprefix("data: ").strip()))
        return events

    def test_matched_destination_creates_history_pinvisit(self):
        location = baker.make("dashboard.Location", latitude="39.204312", longitude="-84.569366")
        pin = baker.make("dashboard.Pin", profile=self.profile, location=location)

        events = self._run(_DIRECTIONS_ENTRY)

        self.assertEqual(events[-1]["type"], "complete")
        self.assertEqual(events[-1]["matched"], 1)
        self.assertEqual(events[-1]["suggested"], 0)
        visit = PinVisit.objects.get(pin=pin)
        self.assertEqual(visit.source, VisitSource.HISTORY)
        pin.refresh_from_db()
        self.assertEqual(pin.last_visited, visit.visited_at)

    def test_matched_destination_idempotent_on_reimport(self):
        location = baker.make("dashboard.Location", latitude="39.204312", longitude="-84.569366")
        baker.make("dashboard.Pin", profile=self.profile, location=location)

        self._run(_DIRECTIONS_ENTRY)
        self._run(_DIRECTIONS_ENTRY)

        self.assertEqual(PinVisit.objects.filter(source=VisitSource.HISTORY).count(), 1)

    def test_unmatched_destination_creates_visit_suggestion(self):
        # No pins at all - nothing near the sample entry's coordinates.
        events = self._run(_DIRECTIONS_ENTRY)

        self.assertEqual(events[-1]["type"], "complete")
        self.assertEqual(events[-1]["matched"], 0)
        self.assertEqual(events[-1]["suggested"], 1)
        suggestion = VisitSuggestion.objects.get(suggested_to=self.profile)
        self.assertTrue(suggestion.from_my_activity)
        self.assertEqual(suggestion.status, VisitSuggestionStatus.PENDING)
        self.assertAlmostEqual(float(suggestion.latitude), 39.204312, places=5)
        self.assertAlmostEqual(float(suggestion.longitude), -84.569366, places=5)
        self.assertIsNone(PinVisit.objects.first())

    def test_unmatched_destination_suggestion_idempotent_on_reimport(self):
        self._run(_DIRECTIONS_ENTRY)
        self._run(_DIRECTIONS_ENTRY)

        self.assertEqual(VisitSuggestion.objects.filter(suggested_to=self.profile, from_my_activity=True).count(), 1)

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    def test_accepting_suggestion_uses_history_visit_source(self, _mock_resolve_name):
        from urbanlens.dashboard.services.visits import accept_visit_suggestion

        self._run(_DIRECTIONS_ENTRY)
        suggestion = VisitSuggestion.objects.get(suggested_to=self.profile)

        # No Location exists at these coordinates, so accepting creates one via
        # _create_location_with_canonical_name(), which resolves a canonical place
        # name from Google - mock that outbound call, same pattern as
        # test_photo_organize.py's CreatePinAndLogVisitTests.
        visit = accept_visit_suggestion(suggestion, self.profile)

        self.assertEqual(visit.source, VisitSource.HISTORY)

    def test_no_entries_yields_error_event(self):
        events = self._run(_SEARCHED_FOR_ENTRY)
        self.assertEqual(events[-1]["type"], "error")

    def test_multiple_files_combined_into_one_pass(self):
        location = baker.make("dashboard.Location", latitude="39.204312", longitude="-84.569366")
        pin = baker.make("dashboard.Pin", profile=self.profile, location=location)

        import json

        files = [
            ("MyActivity.html", _wrap_html(_DIRECTIONS_ENTRY)),
            ("MyActivity (1).html", _wrap_html(_SEARCHED_FOR_ENTRY)),
        ]
        events = [json.loads(line.removeprefix("data: ").strip()) for line in import_my_activity_streaming(files, self.profile)]

        self.assertEqual(events[-1]["type"], "complete")
        self.assertEqual(events[-1]["total"], 1)
        self.assertTrue(PinVisit.objects.filter(pin=pin, source=VisitSource.HISTORY).exists())


class VisitSuggestionFromMyActivityConstraintTests(TestCase):
    """The exactly-one-origin CheckConstraint accepts from_my_activity as a valid 5th origin."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def _base_kwargs(self):
        return {
            "suggested_to": self.profile,
            "latitude": 39.0,
            "longitude": -84.0,
            "visited_at": timezone.now(),
        }

    def test_from_my_activity_alone_is_valid(self):
        suggestion = VisitSuggestion.objects.create(from_my_activity=True, **self._base_kwargs())
        self.assertTrue(suggestion.is_from_my_activity)

    def test_no_origin_set_violates_constraint(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            VisitSuggestion.objects.create(**self._base_kwargs())

    def test_from_my_activity_with_another_origin_violates_constraint(self):
        from django.db import IntegrityError

        origin_visit = baker.make(PinVisit, pin__profile=self.profile)

        with self.assertRaises(IntegrityError):
            VisitSuggestion.objects.create(from_my_activity=True, origin_visit=origin_visit, **self._base_kwargs())
