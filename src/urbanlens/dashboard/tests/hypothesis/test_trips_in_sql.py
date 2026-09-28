"""Trip lists, stat tiles, the month calendar and the trip picker are bounded and computed in SQL."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.trip import TRIP_LIST_PAGE_SIZE, TripPickerView
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.trips.trip_calendar import parse_month, trip_month


def _trip(profile, name: str = "Trip", **kwargs) -> Trip:
    trip = Trip.objects.create(name=name, creator=profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"rsvp": "yes"})
    return trip


class TripSqlTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.today = timezone.now().date()


class TimelineCountsTests(TripSqlTestCase):
    def test_counts_match_each_trips_own_status_in_one_query(self) -> None:
        day = datetime.timedelta(days=1)
        _trip(self.profile, "Planning")
        _trip(self.profile, "Upcoming", start_date=self.today + 5 * day)
        _trip(self.profile, "Active", start_date=self.today - day, end_date=self.today + day)
        _trip(self.profile, "Past", start_date=self.today - 9 * day, end_date=self.today - 8 * day)
        by_activity = _trip(self.profile, "Past by activity")
        TripActivity.objects.create(trip=by_activity, scheduled_at=timezone.now() - 20 * day)
        _trip(baker.make(User).profile, "Someone else's", start_date=self.today)

        mine = Trip.objects.filter(profiles=self.profile)
        with self.assertNumQueries(1):
            counts = mine.timeline_counts()

        expected = {"total": 0, "planning": 0, "upcoming": 0, "active": 0, "past": 0}
        for trip in Trip.objects.filter(profiles=self.profile):
            expected["total"] += 1
            expected[trip.timeline_status] += 1
        self.assertEqual(counts, expected)
        self.assertEqual(counts["past"], 2)


class TripListPagingTests(TripSqlTestCase):
    def test_the_list_renders_one_page_and_the_rest_on_the_next(self) -> None:
        for index in range(TRIP_LIST_PAGE_SIZE + 3):
            _trip(self.profile, f"Trip {index:03d}")

        first = self.client.get(reverse("trips.list"))
        second = self.client.get(reverse("trips.list"), {"page": 2}, HTTP_HX_REQUEST="true")

        self.assertEqual(len(first.context["trips"]), TRIP_LIST_PAGE_SIZE)
        self.assertEqual(len(second.context["trips"]), 3)
        self.assertTemplateUsed(second, "dashboard/partials/trips/trip_list_partial.html")
        self.assertTemplateNotUsed(second, "dashboard/pages/trips/index.html")
        shown = {t.pk for t in first.context["trips"]} | {t.pk for t in second.context["trips"]}
        self.assertEqual(len(shown), TRIP_LIST_PAGE_SIZE + 3)

    def test_soonest_first_pages_do_not_repeat_or_drop_a_trip(self) -> None:
        for index in range(TRIP_LIST_PAGE_SIZE + 3):
            _trip(self.profile, f"Trip {index:03d}", start_date=self.today + datetime.timedelta(days=index % 4))

        seen = []
        for page in (1, 2):
            response = self.client.get(reverse("trips.list"), {"sort": "start_date", "dir": "asc", "page": page})
            seen.extend(t.pk for t in response.context["trips"])

        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), TRIP_LIST_PAGE_SIZE + 3)


class TripMonthTests(TripSqlTestCase):
    def test_only_trips_meeting_the_month_are_read(self) -> None:
        month = self.today.replace(day=1)
        inside = _trip(self.profile, "Inside", start_date=month + datetime.timedelta(days=2))
        spanning = _trip(self.profile, "Spanning", start_date=month - datetime.timedelta(days=3), end_date=month)
        _trip(
            self.profile,
            "Earlier",
            start_date=month - datetime.timedelta(days=60),
            end_date=month - datetime.timedelta(days=40),
        )
        _trip(self.profile, "Undated")

        grid = trip_month(self.profile, month)

        names = {t.name for cell in grid.days for t in cell.trips}
        self.assertEqual(names, {inside.name, spanning.name})
        self.assertEqual(grid.days[(month.weekday() + 1) % 7].day, month)

    def test_query_count_does_not_depend_on_trips_outside_the_month(self) -> None:
        month = self.today.replace(day=1)
        _trip(self.profile, "Inside", start_date=month)

        def cost() -> int:
            with CaptureQueriesContext(connection) as ctx:
                trip_month(self.profile, month)
            return len(ctx.captured_queries)

        before = cost()
        for index in range(15):
            _trip(self.profile, f"Old {index}", start_date=month - datetime.timedelta(days=100 + index))
        self.assertEqual(cost(), before)

    def test_month_endpoint_and_malformed_month(self) -> None:
        self.assertEqual(parse_month("not-a-month", self.today), self.today.replace(day=1))
        self.assertEqual(parse_month("2024-02", self.today), datetime.date(2024, 2, 1))

        response = self.client.get(reverse("trips.calendar.month"), {"month": "2024-02"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "February 2024")
        self.assertContains(response, "month=2024-01")
        self.assertContains(response, "month=2024-03")

    def test_more_than_three_trips_on_a_day_collapse(self) -> None:
        for index in range(5):
            _trip(self.profile, f"Busy {index}", start_date=self.today)

        cell = next(cell for cell in trip_month(self.profile, self.today).days if cell.day == self.today)

        self.assertEqual((len(cell.trips), cell.more), (3, 2))


class TripPickerTests(TripSqlTestCase):
    def test_capped_searchable_and_scoped_to_the_viewer(self) -> None:
        for index in range(TripPickerView.LIMIT + 5):
            _trip(self.profile, f"Road trip {index}")
        _trip(self.profile, "Ridge hike")
        _trip(baker.make(User).profile, "Ridge secret")

        everything = self.client.get(reverse("trips.picker"))
        ridge = self.client.get(reverse("trips.picker"), {"q": "ridge"})

        self.assertEqual(len(everything.context["trips"]), TripPickerView.LIMIT)
        self.assertTrue(everything.context["truncated"])
        self.assertEqual([t.name for t in ridge.context["trips"]], ["Ridge hike"])
        self.assertNotContains(ridge, "Ridge secret")


class ExternalTripsListTests(TripSqlTestCase):
    def setUp(self) -> None:
        super().setUp()
        api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.TRIPS_READ.value])

    def _get(self, **params):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(
                reverse("external_api:trips"), params, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}"
            )
        self.assertEqual(response.status_code, 200)
        return response.json(), len(ctx.captured_queries)

    def test_soonest_first_is_paged_in_sql(self) -> None:
        for index in range(3):
            _trip(self.profile, f"Trip {index}", start_date=self.today + datetime.timedelta(days=index))
        self._get()  # first-request bookkeeping (key last-used, login activity) is not what is measured
        small, small_queries = self._get(sort="start_date", dir="asc", page_size=2)

        for index in range(3, 15):
            _trip(self.profile, f"Trip {index}", start_date=self.today + datetime.timedelta(days=index))
        large, large_queries = self._get(sort="start_date", dir="asc", page_size=2)

        self.assertEqual(large["count"], 15)
        self.assertEqual([row["name"] for row in large["results"]], ["Trip 0", "Trip 1"])
        self.assertEqual(large_queries, small_queries)
        self.assertEqual(small["count"], 3)
