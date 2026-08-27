"""Reordering a trip's activities pushes the change to its synced calendar.

``sync_trip_on_activity_save`` calls ``queue_calendar_push`` so an auto-synced
calendar event follows the trip. It is a ``post_save`` receiver, and
``reorder_activities`` writes each position through ``queryset.update()``, which
fires no ``post_save`` - so the one operation whose entire purpose is changing
activity order never reached the calendar.

Same shape as the label-reorder cache bug: a reorder loop using the one write form
that skips the sync its own model depends on.

The push is queued once per reorder rather than once per row. The receiver fires per
saved activity and ``queue_calendar_push`` takes a trip id, so a row-by-row form
would queue the same trip N times for a single drag.
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.trips.trip_activities import reorder_activities

# Patched where reorder_activities looks it up.
PUSH = "urbanlens.dashboard.services.trips.trip_activities.queue_calendar_push"


class TripReorderSyncsCalendarTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.trip = baker.make(Trip, creator=self.profile, allow_edit_activities="everyone")
        self.activities = [baker.make(TripActivity, trip=self.trip, title=f"Stop {i}", order=i, scheduled_at=None) for i in range(3)]

    def test_reordering_queues_exactly_one_calendar_push(self) -> None:
        new_order = [self.activities[2].pk, self.activities[0].pk, self.activities[1].pk]

        with patch(PUSH) as push:
            reorder_activities(self.trip, self.profile, new_order)

        push.assert_called_once_with(self.trip.pk)

    def test_the_posted_order_is_applied(self) -> None:
        new_order = [self.activities[2].pk, self.activities[0].pk, self.activities[1].pk]

        with patch(PUSH):
            reorder_activities(self.trip, self.profile, new_order)

        applied = list(TripActivity.objects.filter(trip=self.trip).order_by("order").values_list("pk", flat=True))
        self.assertEqual(applied, new_order)

    def test_reordering_costs_a_fixed_number_of_queries(self) -> None:
        """Three activities and six must cost the same, or it is still one UPDATE per row."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with patch(PUSH):
            small = [a.pk for a in reversed(self.activities)]
            with CaptureQueriesContext(connection) as small_ctx:
                reorder_activities(self.trip, self.profile, small)

            extra = [baker.make(TripActivity, trip=self.trip, title=f"Extra {i}", order=10 + i, scheduled_at=None) for i in range(3)]
            allsix = [a.pk for a in reversed(self.activities + extra)]
            with CaptureQueriesContext(connection) as large_ctx:
                reorder_activities(self.trip, self.profile, allsix)

        self.assertEqual(
            len(small_ctx.captured_queries),
            len(large_ctx.captured_queries),
            f"reorder scales with activity count: 3 took {len(small_ctx.captured_queries)}, "
            f"6 took {len(large_ctx.captured_queries)}",
        )
