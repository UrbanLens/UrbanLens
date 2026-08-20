"""What the weather actually was, on the visit surfaces.

REData's ``GET /weather/history/`` reached UrbanLens on the trip weather panel
first; the visit surfaces the design doc named before it stayed empty. A visit
row is where it belongs most - a photograph of a flooded basement means
something different once the row above it says three inches of rain fell that
day.

Two things here are not obvious and are what these tests hold.

**Sparse days are not a range.** ``recorded_range`` fetches ``min..max`` in one
request, which is right for a trip's activities and wrong for a page of visits
to the same ruin: those can span decades, and the range form would fetch and
cache every day in between to display ten. ``recorded_days`` clusters instead.

**The panel never makes the call itself.** It renders a page of visits inline,
and a page render must not block on an outbound request - behind a spinner or
not, a slow REData would hold up the whole visit list for a decorative line of
text. It reads the cache and queues the gap, which is the same
fetch-behind/render-from-cache split every pin-detail panel already uses. Two
unrelated tests found this the hard way, by tripping the suite's
localhost-only network guard the moment the panel started fetching inline.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.locations.visit_weather import RecordedDay, _clusters, recorded_days

_FETCH = "urbanlens.dashboard.services.locations.visit_weather._fetch_days"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


def _row(iso: str) -> dict:
    return {
        "date": iso,
        "temperature_max_c": 20.0,
        "temperature_min_c": 10.0,
        "temperature_mean_c": 15.0,
        "precipitation_mm": 25.4,
        "snowfall_cm": None,
        "wind_speed_max_kmh": 32.1868,
        "wind_gusts_max_kmh": None,
    }


class ClusterTests(SimpleTestCase):
    """Which days get merged into one request, and which do not."""

    def test_consecutive_days_are_one_range(self) -> None:
        days = [datetime.date(2024, 5, day) for day in (1, 2, 3)]

        self.assertEqual(_clusters(days), [(datetime.date(2024, 5, 1), datetime.date(2024, 5, 3))])

    def test_a_nearby_gap_is_bridged(self) -> None:
        """A range is one request however wide, so merging a fortnight is free."""
        days = [datetime.date(2024, 5, 1), datetime.date(2024, 5, 15)]

        self.assertEqual(_clusters(days), [(datetime.date(2024, 5, 1), datetime.date(2024, 5, 15))])

    def test_years_apart_are_separate_requests(self) -> None:
        """The case the range form gets wrong: 7,000 days fetched to show two."""
        days = [datetime.date(2005, 5, 1), datetime.date(2024, 5, 1)]

        self.assertEqual(_clusters(days), [(datetime.date(2005, 5, 1), datetime.date(2005, 5, 1)), (datetime.date(2024, 5, 1), datetime.date(2024, 5, 1))])

    def test_order_and_duplicates_do_not_matter(self) -> None:
        days = [datetime.date(2024, 5, 3), datetime.date(2024, 5, 1), datetime.date(2024, 5, 3)]

        self.assertEqual(_clusters(days), [(datetime.date(2024, 5, 1), datetime.date(2024, 5, 3))])

    def test_no_days_is_no_requests(self) -> None:
        self.assertEqual(_clusters([]), [])


class RecordedDaysTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.location = baker.make(Location, latitude=41.73, longitude=-73.92)

    def test_two_visits_decades_apart_are_two_bounded_requests(self) -> None:
        days = [datetime.date(2005, 5, 1), datetime.date(2024, 5, 1)]

        with patch(_FETCH, side_effect=lambda _lat, _lng, start, _end: {start.isoformat(): _row(start.isoformat())}) as fetch:
            recorded = recorded_days(self.location, days)

        self.assertEqual(fetch.call_count, 2)
        for call in fetch.call_args_list:
            _lat, _lng, start, end = call.args
            self.assertEqual(start, end, "a lone day must not be asked for as a nineteen-year range")
        self.assertEqual(sorted(recorded), ["2005-05-01", "2024-05-01"])

    def test_a_run_of_visits_costs_one_request(self) -> None:
        days = [datetime.date(2024, 5, day) for day in (1, 2, 3)]

        with patch(_FETCH, return_value={day.isoformat(): _row(day.isoformat()) for day in days}) as fetch:
            recorded = recorded_days(self.location, days)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(recorded), 3)

    def test_a_cached_day_is_not_re_fetched(self) -> None:
        day = datetime.date(2024, 5, 1)
        with patch(_FETCH, return_value={day.isoformat(): _row(day.isoformat())}):
            recorded_days(self.location, [day])

        with patch(_FETCH) as fetch:
            recorded = recorded_days(self.location, [day])

        fetch.assert_not_called()
        self.assertEqual(len(recorded), 1)

    def test_a_day_inside_the_publication_lag_is_never_requested(self) -> None:
        with patch(_FETCH) as fetch:
            recorded = recorded_days(self.location, [timezone.now().date()])

        fetch.assert_not_called()
        self.assertEqual(recorded, {})

    def test_an_unavailable_source_answers_nothing_rather_than_raising(self) -> None:
        with patch(_FETCH, return_value={}):
            self.assertEqual(recorded_days(self.location, [datetime.date(2024, 5, 1)]), {})


class FetchTaskTests(TestCase):
    """The Celery half - what the panel queues rather than doing itself."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.location = baker.make(Location, latitude=41.73, longitude=-73.92)

    def test_the_task_fills_the_cache_the_panel_reads(self) -> None:
        from urbanlens.dashboard.tasks import fetch_recorded_weather

        with patch(_FETCH, return_value={"2024-05-01": _row("2024-05-01")}):
            stored = fetch_recorded_weather(self.location.pk, ["2024-05-01"])

        self.assertEqual(stored, 1)
        with patch(_FETCH) as fetch:
            self.assertEqual(len(recorded_days(self.location, [datetime.date(2024, 5, 1)], allow_fetch=False)), 1)
        fetch.assert_not_called()

    def test_a_deleted_location_is_not_an_error(self) -> None:
        from urbanlens.dashboard.tasks import fetch_recorded_weather

        self.assertEqual(fetch_recorded_weather(9_999_999, ["2024-05-01"]), 0)

    def test_a_malformed_date_is_skipped_rather_than_raised(self) -> None:
        """The task's arguments survive a broker round trip as plain strings."""
        from urbanlens.dashboard.tasks import fetch_recorded_weather

        with patch(_FETCH, return_value={"2024-05-01": _row("2024-05-01")}):
            self.assertEqual(fetch_recorded_weather(self.location.pk, ["not-a-date", "2024-05-01"]), 1)

    def test_no_usable_dates_asks_nothing(self) -> None:
        from urbanlens.dashboard.tasks import fetch_recorded_weather

        with patch(_FETCH) as fetch:
            self.assertEqual(fetch_recorded_weather(self.location.pk, ["not-a-date"]), 0)

        fetch.assert_not_called()


class SummaryTests(SimpleTestCase):
    """The one-line description a visit row shows."""

    def test_a_full_day_reads_as_a_sentence(self) -> None:
        day = RecordedDay(day=datetime.date(2024, 5, 1), high_f=72.0, low_f=54.0, precipitation_in=0.3, gust_max_mph=31.0)

        self.assertEqual(day.summary, "72° / 54°F · 0.30 in rain · gusts 31 mph")

    def test_a_dry_day_says_nothing_about_rain(self) -> None:
        """0.00 in rain is what every dry day looks like; it is not information."""
        day = RecordedDay(day=datetime.date(2024, 5, 1), high_f=72.0, low_f=54.0, precipitation_in=0.0, snowfall_in=0.0)

        self.assertEqual(day.summary, "72° / 54°F")

    def test_a_freezing_high_is_still_reported(self) -> None:
        """Zero degrees is a reading, unlike zero rain - the falsy-value trap."""
        day = RecordedDay(day=datetime.date(2024, 1, 1), high_f=0.0, low_f=-11.0)

        self.assertEqual(day.summary, "0° / -11°F")

    def test_a_partial_reading_says_which_half_it_has(self) -> None:
        self.assertEqual(RecordedDay(day=datetime.date(2024, 5, 1), high_f=72.0).summary, "high 72°F")
        self.assertEqual(RecordedDay(day=datetime.date(2024, 5, 1), low_f=54.0).summary, "low 54°F")
        self.assertEqual(RecordedDay(day=datetime.date(2024, 5, 1), mean_f=60.0).summary, "60°F")

    def test_wind_is_only_reported_when_there_are_no_gusts(self) -> None:
        """Two numbers for the same thing reads as noise; the gust is the one that matters."""
        both = RecordedDay(day=datetime.date(2024, 5, 1), wind_max_mph=20.0, gust_max_mph=31.0)
        wind_only = RecordedDay(day=datetime.date(2024, 5, 1), wind_max_mph=20.0)

        self.assertEqual(both.summary, "gusts 31 mph")
        self.assertEqual(wind_only.summary, "wind 20 mph")

    def test_an_empty_day_has_no_summary(self) -> None:
        self.assertEqual(RecordedDay(day=datetime.date(2024, 5, 1)).summary, "")


class VisitHistoryPanelTests(TestCase):
    """The panel itself - one request per location, rendered into the row."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user: User = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client_ = Client()
        self.client_.force_login(self.user)
        self.location = baker.make(Location, latitude=41.73, longitude=-73.92)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def _visit(self, when: datetime.date) -> PinVisit:
        return baker.make(PinVisit, pin=self.pin, visited_at=timezone.make_aware(datetime.datetime(when.year, when.month, when.day, 12, 0)))

    def _url(self) -> str:
        return reverse("pin.visits", args=[self.pin.slug])

    def test_a_cached_day_is_shown_in_the_row(self) -> None:
        visit = self._visit(datetime.date(2024, 5, 1))
        with patch(_FETCH, return_value={"2024-05-01": _row("2024-05-01")}):
            recorded_days(self.location, [datetime.date(2024, 5, 1)])

        with patch(_FETCH) as fetch:
            response = self.client_.get(self._url())

        fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["visit_weather"][visit.pk].high_f, 68.0)
        self.assertIn("68° / 50°F", response.content.decode())

    def test_the_render_never_fetches_and_queues_instead(self) -> None:
        """The property two unrelated tests discovered by tripping the network guard."""
        self._visit(datetime.date(2024, 5, 1))

        with patch(_FETCH) as fetch, patch(_ENQUEUE) as enqueue:
            response = self.client_.get(self._url())

        fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["visit_weather"], {})
        self.assertNotIn("visit-weather", response.content.decode())
        self.assertEqual(enqueue.call_args.args[1:], (self.location.pk, ["2024-05-01"]))

    def test_nothing_is_queued_once_everything_is_cached(self) -> None:
        """Otherwise every render of every visit list re-queues the same days."""
        self._visit(datetime.date(2024, 5, 1))
        with patch(_FETCH, return_value={"2024-05-01": _row("2024-05-01")}):
            recorded_days(self.location, [datetime.date(2024, 5, 1)])

        with patch(_ENQUEUE) as enqueue:
            self.client_.get(self._url())

        enqueue.assert_not_called()

    def test_a_day_inside_the_publication_lag_is_not_queued(self) -> None:
        """It is not missing, it is unanswerable - queueing it would retry forever."""
        self._visit(timezone.now().date())

        with patch(_ENQUEUE) as enqueue:
            self.client_.get(self._url())

        enqueue.assert_not_called()

    def test_visits_at_different_places_are_queued_separately(self) -> None:
        """`?children=1` lists a whole subtree, and those are different places."""
        self._visit(datetime.date(2024, 5, 1))
        elsewhere = baker.make(Location, latitude=42.65, longitude=-73.75)
        child = baker.make(Pin, profile=self.profile, location=elsewhere, parent_pin=self.pin)
        baker.make(PinVisit, pin=child, visited_at=timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with patch(_ENQUEUE) as enqueue:
            response = self.client_.get(f"{self._url()}?children=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(enqueue.call_count, 2, "one job per location, not one per visit")
        self.assertEqual({call.args[1] for call in enqueue.call_args_list}, {self.location.pk, elsewhere.pk})

    def test_a_visit_has_a_place_by_construction(self) -> None:
        """Documents why `_visit_weather` carries no missing-location guard.

        `PinVisit.pin`, `Pin.location` and `Location.latitude`/`longitude` are
        all non-null, so "a visit with nowhere to ask about" is not a state the
        database can hold - and a guard for it would be code no test could
        reach.
        """
        for model, field in ((PinVisit, "pin"), (Pin, "location"), (Location, "latitude"), (Location, "longitude")):
            with self.subTest(field=f"{model.__name__}.{field}"):
                self.assertFalse(model._meta.get_field(field).null)
