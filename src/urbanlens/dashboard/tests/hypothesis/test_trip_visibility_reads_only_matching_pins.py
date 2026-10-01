"""Whether a trip-mate may see a common-pin activity is read from the pins at that activity, not the viewer's whole list.

Every trip page with an activity added under ``COMMON_PIN`` read every location the viewer had ever pinned to decide
whether any of them matched the handful of places on the trip.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import _row_counting_wrapper
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.trips.trip_visibility import viewer_hidden_activity_ids

MORE_PINS = 10


class TripVisibilityReadsOnlyMatchingPinsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile
        self.adder = baker.make(User).profile
        Profile.objects.filter(pk=self.adder.pk).update(trip_pin_location_visibility=VisibilityChoice.COMMON_PIN)
        self.trip = Trip.objects.create(name="Outing", creator=self.adder)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)
        self.placed = 0
        self.shared = self._location()
        self.unshared = self._location()
        baker.make(Pin, profile=self.viewer, location=self.shared)
        for location in (self.shared, self.unshared):
            TripActivity.objects.create(trip=self.trip, added_by=self.adder, location=location, title="Stop")

    def _location(self) -> Location:
        self.placed += 1
        return Location.objects.create(latitude=50 + self.placed * 0.01, longitude=60 + self.placed * 0.01)

    def _hidden_locations(self) -> tuple[set[int], int]:
        activities = list(TripActivity.objects.filter(trip=self.trip).select_related("added_by"))
        totals = [0]
        with connection.execute_wrapper(_row_counting_wrapper(totals)):
            hidden = viewer_hidden_activity_ids(activities, Profile.objects.get(pk=self.viewer.pk))
        return {activity.location_id for activity in activities if activity.id in hidden}, totals[0]

    def test_rows_read_do_not_grow_with_the_viewers_pins(self) -> None:
        hidden, baseline = self._hidden_locations()
        self.assertEqual(hidden, {self.unshared.pk}, "one stop is shared and one is not, or nothing was compared")

        for _ in range(MORE_PINS):
            baker.make(Pin, profile=self.viewer, location=self._location())
        hidden, rows = self._hidden_locations()

        self.assertEqual(hidden, {self.unshared.pk})
        self.assertEqual(rows, baseline, f"{MORE_PINS} more pins read {rows - baseline} more rows")

    def test_a_pin_the_viewer_adds_at_the_stop_reveals_it(self) -> None:
        baker.make(Pin, profile=self.viewer, location=self.unshared)

        hidden, _ = self._hidden_locations()

        self.assertEqual(hidden, set())
