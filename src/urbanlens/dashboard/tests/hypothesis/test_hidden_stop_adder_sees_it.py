"""A stop its adder hid from the map stays hidden from every other member, and stays visible, marked, to the adder."""

from __future__ import annotations

import datetime as dt

from django.contrib.auth.models import User

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.trips.trip_activities import build_activity_rows, create_activity
from urbanlens.dashboard.services.trips.trip_map import build_trip_map_points

_WHEN = dt.datetime(2026, 11, 6, 9, 0, tzinfo=dt.UTC)


class AHiddenStopTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.adder = Profile.objects.get(user=User.objects.create_user(username="stop-adder"))
        self.mate = Profile.objects.get(user=User.objects.create_user(username="stop-mate"))
        self.trip = Trip.objects.create(
            name="Weekend", creator=self.mate, start_date=dt.date(2026, 11, 6), end_date=dt.date(2026, 11, 8)
        )
        for profile in (self.adder, self.mate):
            TripMembership.objects.create(
                trip=self.trip, profile=profile, status=TripMembership.STATUS_JOINED, rsvp="yes"
            )
        self.visible = self._stop("Gate", "42.380", by=self.mate)
        self.hidden = self._stop("Boiler house", "42.390", by=self.adder, location_hidden=True)
        self.after = self._stop("Diner", "42.400", by=self.mate)

    def _stop(self, title: str, lat: str, *, by: Profile, location_hidden: bool = False) -> TripActivity:
        return create_activity(
            self.trip,
            by,
            title=title,
            place={"geocoded_lat": lat, "geocoded_lng": "-83.035", "geocoded_name": title},
            scheduled_at=_WHEN,
            location_hidden=location_hidden,
        )

    def _row(self, viewer: Profile, activity: TripActivity) -> dict:
        return next(
            row
            for row in build_activity_rows(self.trip, viewer, include_legs=False)
            if row["activity"].pk == activity.pk
        )

    def _points(self, viewer: Profile) -> dict[int | None, dict]:
        return {point["activity_id"]: point for point in build_trip_map_points(self.trip, viewer)}

    def test_the_adder_sees_its_place_marked_as_hidden_from_others(self) -> None:
        row = self._row(self.adder, self.hidden)
        self.assertFalse(row["effective_location_hidden"])
        self.assertTrue(row["hidden_from_others"])
        self.assertEqual(row["display_location_name"], self.hidden.location.display_name)

        point = self._points(self.adder)[self.hidden.pk]
        self.assertTrue(point["hidden_from_others"])
        self.assertIsNone(point["index"], "a hidden stop must not take a number the other members' list does not show")

    def test_every_other_member_still_cannot_see_it(self) -> None:
        row = self._row(self.mate, self.hidden)
        self.assertTrue(row["effective_location_hidden"])
        self.assertFalse(row["hidden_from_others"])
        self.assertEqual(row["display_location_name"], "")
        self.assertNotIn(self.hidden.pk, self._points(self.mate))

    def test_every_member_sees_the_same_numbers_on_the_map_and_in_the_list(self) -> None:
        for viewer in (self.adder, self.mate):
            with self.subTest(viewer=viewer.user.username):
                points = self._points(viewer)
                for activity in (self.visible, self.after):
                    self.assertEqual(points[activity.pk]["index"], self._row(viewer, activity)["index"])
                self.assertEqual(points[self.after.pk]["index"], 2)
