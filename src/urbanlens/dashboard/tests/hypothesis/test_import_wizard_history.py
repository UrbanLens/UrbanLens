"""Location History, My Activity and GPX tracks go through the live import wizard: preview, then confirm.

The preview parses in the sandbox and the confirmed import runs on a worker, both under ``deny``, so a
parser reached from the confirm side raises rather than passing quietly.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.routes.model import Route, RouteSource
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute, gpx_tracks_to_routes
from urbanlens.dashboard.services.pins import confirmed_import, import_preview
from urbanlens.dashboard.services.pins.history_import import ImportedHistory

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"

LAT, LNG = 42.6526, -73.7562


def _e7(value: float) -> int:
    return int(value * 1e7)


def _timeline() -> bytes:
    """One placeVisit at (LAT, LNG) and one activitySegment with a timestamped path."""
    visit = {
        "placeVisit": {
            "visitConfidence": 100,
            "location": {"latitudeE7": _e7(LAT), "longitudeE7": _e7(LNG), "name": "Old Mill", "placeId": "abc"},
            "duration": {"startTimestamp": "2026-01-01T10:00:00+00:00", "endTimestamp": "2026-01-01T11:00:00+00:00"},
        },
    }
    points = [
        {
            "latE7": _e7(LAT + step / 1000),
            "lngE7": _e7(LNG + step / 1000),
            "timestamp": f"2026-01-01T12:0{step}:00+00:00",
        }
        for step in range(3)
    ]
    segment = {
        "activitySegment": {
            "duration": {"startTimestamp": "2026-01-01T12:00:00+00:00", "endTimestamp": "2026-01-01T12:02:00+00:00"},
            "distance": 321,
            "simplifiedRawPath": {"points": points},
        },
    }
    return json.dumps({"timelineObjects": [visit, segment]}).encode()


_DIRECTIONS_ENTRY = (
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Maps<br></p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Directions to '
    '<a href="https://www.google.com/maps/dir//39.2043118,-84.5693664/">2360 Kipling Ave</a><br>Current location<br>'
    "39.2043118,-84.56936639999999<br>Jul 3, 2026, 1:18:25 PM EDT<br></div></div></div>"
)

MY_ACTIVITY = (
    f"<!DOCTYPE html><html><head><title>My Activity</title></head><body>{_DIRECTIONS_ENTRY}</body></html>".encode()
)

GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="40.5" lon="-73.5"><name>Trailhead</name></wpt>
  <trk>
    <name>Evening walk</name>
    <trkseg>
      <trkpt lat="40.000" lon="-73.000"><time>2024-01-01T10:00:00Z</time></trkpt>
      <trkpt lat="40.001" lon="-73.001"><time>2024-01-01T10:05:00Z</time></trkpt>
      <trkpt lat="40.002" lon="-73.002"><time>2024-01-01T10:10:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


class ImportWizardHistoryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.addCleanup(single_flight.release, import_preview.guard_key(self.profile.pk))
        self.addCleanup(single_flight.release, confirmed_import.guard_key(self.profile.pk))

    def _pin_at(self, latitude: float, longitude: float) -> Pin:
        location = baker.make(
            Location, latitude=latitude, longitude=longitude, point=Point(longitude, latitude, srid=4326)
        )
        return baker.make(Pin, profile=self.profile, location=location)

    def _preview(self, name: str, data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
        upload = SimpleUploadedFile(name, data, content_type="application/octet-stream")
        with (
            override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(ENQUEUE, return_value=mock.Mock()),
        ):
            response = self.client.post(reverse("pin.import.preview"), {"upload_files": [upload]})
        self.assertEqual(response.status_code, 202, response.content)
        job = response.json()
        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(ENQUEUE, return_value=mock.Mock()),
        ):
            import_preview.parse_import_preview(self.profile.pk, job["job_id"])
        return job, self.client.get(job["status_url"]).json()

    def _confirm(self, job: dict[str, Any], state: dict[str, Any], **overrides: Any) -> Any:
        lists = [
            {"stem": entry["stem"], "create_category": False, "label_ids": [], "pins": entry["pins"]}
            for entry in (state.get("result") or {}).get("lists", [])
        ]
        body = {"lists": lists, "auto_tag": False, "preview_id": job["job_id"], **overrides}
        with mock.patch(ENQUEUE, return_value=mock.Mock()):
            return self.client.post(
                reverse("pin.import.confirmed"), data=json.dumps(body), content_type="application/json"
            )

    def _run(self, response: Any) -> dict[str, Any]:
        job = response.json()
        with override_settings(UL_PROCESS_ROLE="worker", UL_UNTRUSTED_PARSE_POLICY="deny"):
            confirmed_import.run_confirmed_import(self.profile.pk, job["job_id"])
        return self.client.get(job["status_url"]).json()

    def _import(self, name: str, data: bytes) -> dict[str, Any] | None:
        """Preview then confirm everything the preview offered; the final import status, if one started."""
        job, state = self._preview(name, data)
        if state.get("status") != "done":
            return None
        response = self._confirm(job, state)
        if response.status_code != 202:
            return None
        return self._run(response)

    def test_a_semantic_location_history_file_logs_visits_and_saves_its_trips(self) -> None:
        pin = self._pin_at(LAT, LNG)

        self._import("2026_JANUARY.json", _timeline())

        self.assertEqual(PinVisit.objects.filter(pin=pin, source=VisitSource.HISTORY).count(), 1)
        route = Route.objects.get(profile=self.profile)
        self.assertEqual(route.source, RouteSource.GOOGLE_TAKEOUT_SEMANTIC)
        self.assertEqual(route.distance_meters, 321)

    def test_a_my_activity_file_suggests_the_places_it_found(self) -> None:
        self._import("MyActivity.html", MY_ACTIVITY)

        suggestion = VisitSuggestion.objects.get(suggested_to=self.profile)
        self.assertTrue(suggestion.from_my_activity)

    def test_a_my_activity_file_logs_a_visit_at_a_pin_it_matches(self) -> None:
        pin = self._pin_at(39.204312, -84.569366)

        self._import("MyActivity.html", MY_ACTIVITY)

        self.assertEqual(PinVisit.objects.filter(pin=pin, source=VisitSource.HISTORY).count(), 1)
        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=self.profile).exists())

    def test_a_gpx_file_saves_its_track_as_a_route_and_its_waypoints_as_pins(self) -> None:
        self._import("walk.gpx", GPX)

        route = Route.objects.get(profile=self.profile)
        self.assertEqual(route.source, RouteSource.GPX_TRACK)
        self.assertEqual(route.name, "Evening walk")
        self.assertEqual(route.raw_point_count, 3)
        self.assertTrue(Pin.objects.filter(profile=self.profile, name="Trailhead").exists())

    def test_a_gpx_track_near_a_pin_records_the_dwell_as_a_visit(self) -> None:
        pin = self._pin_at(40.001, -73.001)
        slow = GPX.replace(b"10:05:00Z", b"10:15:00Z").replace(b"10:10:00Z", b"10:30:00Z")
        slow = slow.replace(b'lat="40.000" lon="-73.000"', b'lat="40.001" lon="-73.001"')

        self._import("walk.gpx", slow)

        self.assertEqual(PinVisit.objects.filter(pin=pin, source=VisitSource.HISTORY).count(), 1)

    def test_the_preview_counts_what_it_found_without_sending_it_to_the_browser(self) -> None:
        _job, state = self._preview("2026_JANUARY.json", _timeline())

        self.assertEqual(state["status"], "done", state)
        result = state["result"]
        self.assertEqual(result["lists"], [])
        self.assertEqual(result["history"], {"visits": 1, "activity": 0, "routes": 1})
        self.assertTrue(result["history_summary"])
        self.assertNotIn("latE7", json.dumps(state))
        self.assertNotIn("points", json.dumps(result))

    def test_route_tracking_off_saves_no_routes_but_still_logs_visits(self) -> None:
        self.profile.track_routes = False
        self.profile.save(update_fields=["track_routes"])
        pin = self._pin_at(LAT, LNG)

        self._import("2026_JANUARY.json", _timeline())

        self.assertFalse(Route.objects.filter(profile=self.profile).exists())
        self.assertEqual(PinVisit.objects.filter(pin=pin, source=VisitSource.HISTORY).count(), 1)

    def test_visit_logging_off_logs_no_visits_but_still_saves_routes(self) -> None:
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        self._pin_at(LAT, LNG)

        status = self._import("2026_JANUARY.json", _timeline())

        self.assertFalse(PinVisit.objects.exists())
        self.assertEqual(Route.objects.filter(profile=self.profile).count(), 1)
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "done", status)

    def test_a_preview_is_imported_once(self) -> None:
        """Routes are not deduplicated, so a second confirm of the same preview would save every trip twice."""
        job, state = self._preview("walk.gpx", GPX)
        self._run(self._confirm(job, state))

        again = self._confirm(job, state)

        self.assertEqual(again.status_code, 410, again.content)
        self.assertEqual(Route.objects.filter(profile=self.profile).count(), 1)
        self.assertIsNone(single_flight.holder(confirmed_import.guard_key(self.profile.pk)))

    def test_history_can_be_left_out_of_the_import(self) -> None:
        job, state = self._preview("walk.gpx", GPX)

        self._run(self._confirm(job, state, preview_id=None))

        self.assertFalse(Route.objects.exists())
        self.assertTrue(Pin.objects.filter(profile=self.profile, name="Trailhead").exists())

    def test_someone_elses_preview_cannot_be_imported(self) -> None:
        job, state = self._preview("2026_JANUARY.json", _timeline())
        self.client.force_login(baker.make(User))

        response = self._confirm(job, state)

        self.assertEqual(response.status_code, 410, response.content)
        self.assertFalse(Route.objects.exists())

    def test_a_preview_id_that_is_not_one_is_refused(self) -> None:
        for preview_id in ("../../etc", "not-a-uuid", 7):
            with self.subTest(preview_id=preview_id), mock.patch(ENQUEUE) as enqueue:
                response = self.client.post(
                    reverse("pin.import.confirmed"),
                    data=json.dumps({"lists": [], "preview_id": preview_id}),
                    content_type="application/json",
                )

                self.assertIn(response.status_code, (400, 410), response.content)
                enqueue.assert_not_called()
                self.assertIsNone(single_flight.holder(confirmed_import.guard_key(self.profile.pk)))


class ImportedHistoryTests(TestCase):
    """What the preview stores for the confirmed import survives being written down and read back."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_a_gpx_track_round_trips_through_its_stored_form(self) -> None:
        (parsed,) = gpx_tracks_to_routes(GPX, self.profile, "walk.gpx")

        rebuilt = ParsedRoute.from_json(json.loads(json.dumps(parsed.to_json())), self.profile)

        for field in ("name", "source", "source_filename", "raw_point_count", "simplified_point_count", "started_at"):
            self.assertEqual(getattr(rebuilt.route, field), getattr(parsed.route, field), field)
        self.assertAlmostEqual(rebuilt.route.distance_meters, parsed.route.distance_meters)
        self.assertEqual(rebuilt.route.path.coords, parsed.route.path.coords)
        self.assertEqual(rebuilt.raw_points, parsed.raw_points)
        self.assertEqual(rebuilt.route.profile, self.profile)

    def test_an_untimed_track_keeps_no_raw_points(self) -> None:
        """Dwell detection needs timestamps, so an untimed track's points would be stored for nothing."""
        untimed = GPX.replace(b"<time>", b"<!--").replace(b"</time>", b"-->")
        (parsed,) = gpx_tracks_to_routes(untimed, self.profile, "walk.gpx")

        self.assertEqual(parsed.to_json()["points"], [])

    def test_several_my_activity_files_add_up(self) -> None:
        history = ImportedHistory()
        history.add_my_activity(MY_ACTIVITY)
        history.add_my_activity(MY_ACTIVITY.replace(b"Jul 3, 2026", b"Jul 4, 2026"))

        self.assertEqual(history.counts(), {"visits": 0, "activity": 2, "routes": 0})
        stored = ImportedHistory.from_json(json.loads(json.dumps(history.to_json())))
        list(stored.iter_import_events(self.profile))
        self.assertEqual(VisitSuggestion.objects.filter(suggested_to=self.profile).count(), 2)

    def test_a_file_that_is_not_a_timeline_is_refused(self) -> None:
        for data in (b"[]", b'{"timelineObjects": "nope"}', b"not json"):
            with self.subTest(data=data), self.assertRaises((TypeError, ValueError)):
                ImportedHistory().add_location_history(data, self.profile, "x.json")


class ParseThenConfirmTests(TestCase):
    """Pin files end to end through the live path: parse_for_preview, then iter_confirmed_import_events."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _import(self, name: str, data: bytes) -> list[dict[str, Any]]:
        parse = self.gateway.parse_for_preview([(name, data)], self.profile)
        lists = [{**entry, "create_category": False, "label_ids": []} for entry in parse.lists]
        with mock.patch(ENQUEUE, return_value=mock.Mock()):
            return list(self.gateway.iter_confirmed_import_events(lists, self.profile, auto_tag=False))

    def test_a_coordinates_only_csv_imports_every_row_with_or_without_quotes_and_a_bom(self) -> None:
        rows = [(34.0 + index / 10, -80.0 - index / 10) for index in range(21)]
        plain = "latitude,longitude\n" + "".join(f"{lat},{lng}\n" for lat, lng in rows)
        quoted = '"latitude","longitude"\n' + "".join(f'"{lat}","{lng}"\n' for lat, lng in rows)
        for label, payload in (
            ("plain", plain.encode()),
            ("quoted", quoted.encode()),
            ("bom", b"\xef\xbb\xbf" + quoted.encode()),
        ):
            with self.subTest(label=label):
                Pin.objects.filter(profile=self.profile).delete()

                events = self._import("coords.csv", payload)

                self.assertEqual(events[-1]["type"], "complete", events[-1])
                self.assertEqual(events[-1]["created"], 21, events[-1])
                self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 21)

    def test_a_takeout_url_row_is_deferred_for_its_lookup_rather_than_failing(self) -> None:
        csv_bytes = (
            b"Title,URL\n"
            b'Black Point Ruins,"https://www.google.com/maps/place/Black+Point+Ruins/data=!4m2!3m1!1s0x89e5bd8b55e7f8fd:0x59ac8820518a7e79"\n'
        )

        events = self._import("pins.csv", csv_bytes)

        complete = next(event for event in events if event["type"] == "complete")
        self.assertEqual(complete["skipped"], 0, complete)
        self.assertEqual(complete["created"] + complete["deferred"], 1, complete)


class GetNearbyOrCreateProfileDefaultsTests(TestCase):
    """get_nearby_or_create() tolerates parser dicts that carry a ``profile`` key."""

    def test_profile_in_defaults_does_not_conflict_with_argument(self) -> None:
        profile = baker.make(User).profile

        pin, created = Pin.objects.get_nearby_or_create(
            40.0, -74.0, profile, defaults={"profile": profile, "name": "Old Mill"}
        )

        self.assertTrue(created)
        self.assertEqual(pin.profile, profile)
        self.assertEqual(pin.name, "Old Mill")
