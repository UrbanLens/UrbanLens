"""Tests for the trip source of the Memories feed.

Two things are pinned here. First, the feed must not scale its query count with
the number of trips: each trip used to re-derive its own start date, end date,
and representative point with a query apiece, on top of the one query that
already selected them.

Second, the range filter and the rendered date have to agree on when a trip
ends. The filter annotates the last activity date; ``Trip.effective_end_date``
also considers ``scheduled_end``. A trip whose final activity *runs past* the
last start time falls in the gap between those two definitions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.memories.aggregator import get_memory_events


def _aware(value: datetime) -> datetime:
    return timezone.make_aware(value) if timezone.is_naive(value) else value


class TripMemoryAggregationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def _trip_with_activity(self, *, name: str, start: datetime, end: datetime | None = None) -> Trip:
        trip = baker.make(Trip, name=name, start_date=None, end_date=None)
        trip.profiles.add(self.profile)
        baker.make(TripActivity, trip=trip, scheduled_at=_aware(start), scheduled_end=_aware(end) if end else None, lat_override=40.0, lng_override=-74.0)
        return trip

    def test_query_count_does_not_grow_with_the_number_of_trips(self) -> None:
        window_start, window_end = date(2026, 3, 1), date(2026, 3, 31)
        self._trip_with_activity(name="one", start=datetime(2026, 3, 5, 9))

        with CaptureQueriesContext(connection) as one_trip:
            get_memory_events(self.profile, window_start, window_end)

        for index in range(2, 7):
            self._trip_with_activity(name=f"trip-{index}", start=datetime(2026, 3, 5 + index, 9))

        with CaptureQueriesContext(connection) as six_trips:
            events = get_memory_events(self.profile, window_start, window_end)

        self.assertEqual(len([event for event in events if event.type == "trip"]), 6)
        self.assertEqual(len(six_trips.captured_queries), len(one_trip.captured_queries))

    def test_a_trip_whose_last_activity_runs_past_the_window_start_is_included(self) -> None:
        """The activity starts before the window and ends inside it.

        Filtering on scheduled_at alone puts this trip's end before the window and drops
        it, while effective_end_date - which the feed displays - says it is still running.
        """
        self._trip_with_activity(name="long haul", start=datetime(2026, 2, 25, 9), end=datetime(2026, 3, 4, 17))

        events = get_memory_events(self.profile, date(2026, 3, 1), date(2026, 3, 31))

        self.assertEqual([event.title for event in events if event.type == "trip"], ["long haul"])

    def test_reported_dates_match_the_model_properties(self) -> None:
        trip = self._trip_with_activity(name="matches", start=datetime(2026, 3, 5, 9), end=datetime(2026, 3, 8, 17))

        events = [event for event in get_memory_events(self.profile, date(2026, 3, 1), date(2026, 3, 31)) if event.type == "trip"]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].occurred_at.date(), trip.effective_start_date)
        self.assertEqual(events[0].ended_at.date(), trip.effective_end_date)

    def test_an_explicit_date_range_still_wins_over_activities(self) -> None:
        trip = baker.make(Trip, name="explicit", start_date=date(2026, 3, 10), end_date=date(2026, 3, 12))
        trip.profiles.add(self.profile)
        baker.make(TripActivity, trip=trip, scheduled_at=_aware(datetime(2026, 3, 20, 9)), lat_override=40.0, lng_override=-74.0)

        events = [event for event in get_memory_events(self.profile, date(2026, 3, 1), date(2026, 3, 31)) if event.type == "trip"]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].occurred_at.date(), date(2026, 3, 10))
        self.assertEqual(events[0].ended_at.date(), date(2026, 3, 12))

    def test_a_trip_outside_the_window_is_still_excluded(self) -> None:
        self._trip_with_activity(name="last year", start=datetime(2025, 3, 5, 9), end=datetime(2025, 3, 6, 9))

        events = [event for event in get_memory_events(self.profile, date(2026, 3, 1), date(2026, 3, 31)) if event.type == "trip"]

        self.assertEqual(events, [])
