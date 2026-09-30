"""The trip weather panel reads stored weather and fetches forecasts under the request-path policy.

N29 G5-26/G6-2/G2-14 (forecasts fetched per coordinate on the request with no deadline, even for activities
weeks outside any forecast) and G5-27 (history built every calendar day between the earliest and latest
activity, ``scheduled_at`` had no minimum, and a place's recorded days were one JSON document read whole).
"""

from __future__ import annotations

import datetime
import importlib
import threading
from unittest.mock import patch

from django.apps import apps as django_apps
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.cache.recorded_weather import RecordedWeatherDay
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis import request_upstreams
from urbanlens.dashboard.services.locations.visit_weather import cached_records, recorded_days_at, weather_cell
from urbanlens.dashboard.services.trips.trip_errors import TripValidationError

_FETCH_DAYS = "urbanlens.dashboard.services.locations.visit_weather._fetch_days"
_FORECAST = "urbanlens.dashboard.services.apis.locations.weather_resolution.get_raw_forecast_slots"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


def _history_row(iso: str) -> dict:
    return {"date": iso, "temperature_max_c": 20.0, "temperature_min_c": 10.0}


class _TripCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        request_upstreams.WeatherForecastUpstream.reset()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        self.client_ = Client()
        self.client_.force_login(self.user)
        self.trip = Trip.objects.create(name="Trip", creator=self.profile)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.profile, defaults={"rsvp": "yes"})
        self.url = reverse("trips.weather", args=[self.trip.slug])

    def _activity(self, when: datetime.datetime | None, lat: float = 41.73, lng: float = -73.92) -> TripActivity:
        return baker.make(
            TripActivity,
            trip=self.trip,
            title="Stop",
            scheduled_at=when,
            lat_override=lat,
            lng_override=lng,
            pin=None,
            location=None,
        )


class HistoryIsReadNotFetchedTests(_TripCase):
    def setUp(self) -> None:
        super().setUp()
        configured = patch(
            "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=True
        )
        configured.start()
        self.addCleanup(configured.stop)

    def test_with_no_redata_nothing_is_queued_or_awaited(self) -> None:
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with (
            patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=False,
            ),
            patch(_ENQUEUE) as enqueue,
        ):
            resp = self.client_.get(self.url)

        enqueue.assert_not_called()
        self.assertEqual(resp.context["refresh_url"], "")

    def test_a_missing_day_is_queued_and_the_request_does_not_fetch(self) -> None:
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with patch(_FETCH_DAYS) as fetch, patch(_ENQUEUE) as enqueue:
            resp = self.client_.get(self.url)

        fetch.assert_not_called()
        enqueue.assert_called_once()
        task, latitude, longitude, iso_days = enqueue.call_args.args
        self.assertEqual(task.name.rsplit(".", 1)[-1], "fetch_recorded_weather_at")
        self.assertEqual((latitude, longitude, iso_days), (41.73, -73.92, ["2024-05-01"]))
        self.assertEqual(resp.context["recorded_days"], [])
        self.assertTrue(resp.context["refresh_url"], "the panel asks again once while the day is arriving")

    def test_the_retry_does_not_ask_again(self) -> None:
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with patch(_ENQUEUE):
            resp = self.client_.get(self.url, {"retry": "1"})

        self.assertEqual(resp.context["refresh_url"], "")

    def test_reloading_queues_the_same_fetch_once(self) -> None:
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with patch(_ENQUEUE) as enqueue:
            self.client_.get(self.url)
            self.client_.get(self.url)

        self.assertEqual(enqueue.call_count, 1)

    def test_activities_decades_apart_never_become_one_span(self) -> None:
        """The G5-27 case: 1940 and today used to be one request for every day between them."""
        from urbanlens.dashboard.tasks import fetch_recorded_weather_at

        self._activity(timezone.make_aware(datetime.datetime(1941, 5, 1, 12, 0)))
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with patch(_FETCH_DAYS, return_value={}) as fetch, tasks_run_inline(fetch_recorded_weather_at):
            self.client_.get(self.url)

        spans = [(call.args[3] - call.args[2]).days for call in fetch.call_args_list]
        self.assertEqual(spans, [0, 0])

    def test_stored_days_are_shown_without_queuing_anything(self) -> None:
        cell = weather_cell(41.73, -73.92)
        RecordedWeatherDay.objects.create(
            cell_lat=cell[0], cell_lng=cell[1], day=datetime.date(2024, 5, 1), data=_history_row("2024-05-01")
        )
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))

        with patch(_ENQUEUE) as enqueue:
            resp = self.client_.get(self.url)

        enqueue.assert_not_called()
        self.assertEqual(len(resp.context["recorded_days"]), 1)
        self.assertEqual(resp.context["refresh_url"], "")


class RecordedDaysAreRowsTests(TestCase):
    def test_each_day_is_its_own_row_shared_by_nearby_places(self) -> None:
        days = {"2024-05-01": _history_row("2024-05-01"), "2024-05-02": _history_row("2024-05-02")}
        with patch(_FETCH_DAYS, return_value=days):
            recorded_days_at(41.731, -73.921, [datetime.date(2024, 5, 1), datetime.date(2024, 5, 2)])

        self.assertEqual(RecordedWeatherDay.objects.count(), 2)
        with patch(_FETCH_DAYS) as fetch:
            again = recorded_days_at(41.7312, -73.9208, [datetime.date(2024, 5, 1)])
        fetch.assert_not_called()
        self.assertEqual(list(again), ["2024-05-01"])

    def test_reading_some_days_loads_only_those_rows(self) -> None:
        cell = weather_cell(41.73, -73.92)
        RecordedWeatherDay.objects.bulk_create(
            [
                RecordedWeatherDay(
                    cell_lat=cell[0],
                    cell_lng=cell[1],
                    day=datetime.date(2000, 1, 1) + datetime.timedelta(days=i),
                    data={"n": i},
                )
                for i in range(200)
            ]
        )

        found = cached_records({cell: [datetime.date(2000, 1, 5)]})

        self.assertEqual(list(found[cell]), ["2000-01-05"])

    def test_the_old_documents_move_to_rows(self) -> None:
        location = baker.make("dashboard.Location", latitude=41.73, longitude=-73.92)
        LocationCache.objects.create(
            location=location,
            source="redata_weather_history",
            data={"2024-05-01": _history_row("2024-05-01"), "junk": 1},
        )
        migration = importlib.import_module("urbanlens.dashboard.migrations.0032_v0_8_0")

        migration._0091__move_to_day_rows(django_apps, None)

        row = RecordedWeatherDay.objects.get()
        self.assertEqual((row.cell_lat, row.cell_lng, row.day), (4173, -7392, datetime.date(2024, 5, 1)))
        self.assertFalse(LocationCache.objects.filter(source="redata_weather_history").exists())


class ForecastTests(_TripCase):
    def _slot(self, when: datetime.datetime) -> dict:
        return {"date": when.replace(tzinfo=None), "temp": 20, "condition": "Clear", "icon": "wb_sunny"}

    def test_an_activity_past_every_forecast_is_not_fetched(self) -> None:
        self._activity(timezone.now() + datetime.timedelta(days=40))

        with patch(_FORECAST) as forecast:
            self.client_.get(self.url)

        forecast.assert_not_called()

    def test_a_place_forecast_is_reused_across_requests(self) -> None:
        when = timezone.now() + datetime.timedelta(days=1)
        self._activity(when)

        with patch(_FORECAST, return_value=[self._slot(when)]) as forecast:
            first = self.client_.get(self.url)
            second = self.client_.get(self.url)

        self.assertEqual(forecast.call_count, 1)
        self.assertTrue(first.context["grouped"])
        self.assertTrue(second.context["grouped"])

    def test_a_failed_forecast_is_not_cached(self) -> None:
        import requests

        when = timezone.now() + datetime.timedelta(days=1)
        self._activity(when)

        with patch(_FORECAST, side_effect=[requests.ConnectionError("down"), [self._slot(when)]]) as forecast:
            self.client_.get(self.url)
            second = self.client_.get(self.url)

        self.assertEqual(forecast.call_count, 2)
        self.assertTrue(second.context["grouped"])

    def test_a_hanging_forecast_costs_the_request_only_the_deadline(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        self._activity(timezone.now() + datetime.timedelta(days=1))

        def _hang(*_args):
            release.wait(timeout=10)
            return []

        with (
            patch.object(request_upstreams.WeatherForecastUpstream, "deadline", 0.2),
            patch(_FORECAST, side_effect=_hang),
        ):
            resp = self.client_.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["refresh_url"])

    def test_query_count_does_not_grow_with_activities(self) -> None:
        when = timezone.now() + datetime.timedelta(days=1)
        self._activity(when)
        self._activity(timezone.make_aware(datetime.datetime(2024, 5, 1, 12, 0)))
        with (
            patch(_FORECAST, return_value=[self._slot(when)]),
            patch(_ENQUEUE),
            CaptureQueriesContext(connection) as few,
        ):
            self.client_.get(self.url)

        for day in range(2, 12):
            self._activity(when, lat=40 + day, lng=-70 - day)
            self._activity(timezone.make_aware(datetime.datetime(2024, 5, day, 12, 0)), lat=30 + day, lng=-80 - day)
        with (
            patch(_FORECAST, return_value=[self._slot(when)]),
            patch(_ENQUEUE),
            CaptureQueriesContext(connection) as many,
        ):
            self.client_.get(self.url)

        self.assertEqual(len(many.captured_queries), len(few.captured_queries))


class ScheduleSpanTests(_TripCase):
    def test_creating_an_activity_in_1800_is_refused(self) -> None:
        from urbanlens.dashboard.services.trips.trip_activities import create_activity

        with self.assertRaises(TripValidationError):
            create_activity(
                self.trip, self.profile, title="x", scheduled_at=timezone.make_aware(datetime.datetime(1800, 1, 1))
            )

    def test_editing_an_end_into_2300_is_refused(self) -> None:
        from urbanlens.dashboard.services.trips.trip_activities import update_activity

        activity = self._activity(None)
        with self.assertRaises(TripValidationError):
            update_activity(
                self.trip,
                self.profile,
                activity.pk,
                changes={"scheduled_end": timezone.make_aware(datetime.datetime(2300, 1, 1))},
            )

    def test_dragging_to_year_one_is_refused(self) -> None:
        from urbanlens.dashboard.services.trips.trip_activities import move_activity

        activity = self._activity(None)
        with self.assertRaises(TripValidationError):
            move_activity(self.trip, self.profile, activity.pk, date=datetime.date(1, 1, 2))

    def test_the_database_refuses_what_the_services_would(self) -> None:
        activity = self._activity(None)
        TripActivity.objects.filter(pk=activity.pk).update(
            scheduled_at=timezone.make_aware(datetime.datetime(2024, 5, 1))
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            TripActivity.objects.filter(pk=activity.pk).update(
                scheduled_at=timezone.make_aware(datetime.datetime(1850, 1, 1))
            )

    def test_the_edit_endpoint_answers_400(self) -> None:
        activity = self._activity(None)
        resp = self.client_.post(
            reverse("trips.activity.edit", args=[self.trip.slug, activity.pk]),
            {"scheduled_date": "1850-01-01", "scheduled_time": "10:00"},
        )

        self.assertEqual(resp.status_code, 400)


class CalendarEventTimesTests(SimpleTestCase):
    def test_an_event_no_activity_could_hold_imports_without_a_time(self) -> None:
        from urbanlens.dashboard.services.trips.calendar_sync import _parse_event_datetime

        self.assertIsNone(_parse_event_datetime({"dateTime": "1850-06-01T10:00:00+00:00"}))
        self.assertIsNotNone(_parse_event_datetime({"dateTime": "2026-06-01T10:00:00+00:00"}))
