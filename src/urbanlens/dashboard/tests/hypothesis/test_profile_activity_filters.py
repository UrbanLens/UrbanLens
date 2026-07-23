"""Tests for the own-profile "private activity" filters.

Covers two bugs found in the private activity panel's strips (now rendered
on the Home overview page):

- "High-priority places to visit" only excluded pins by ``last_visited``,
  missing pins that have a dated ``PinVisit`` but whose ``last_visited``
  never got synced (e.g. bulk-import paths that create ``PinVisit`` rows
  without calling ``sync_last_visited``).
- "Recent trips" showed any trip ordered by ``Trip.updated`` instead of only
  past trips with a comment posted in the last 7 days.
"""

from __future__ import annotations

import datetime
import itertools

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _aware(year: int, month: int, day: int) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime(year, month, day, 12, 0, 0))


def _make_pin(profile, *, last_visited=None, priority=5, name="Priority Spot") -> Pin:
    offset = next(_COORDS)
    location = baker.make("dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}")
    return baker.make("dashboard.Pin", profile=profile, location=location, last_visited=last_visited, priority=priority, name=name)


class PriorityUnvisitedPinsTests(TestCase):
    """The "High-priority places to visit" widget on the Home overview page.

    The dashboard (and its context) moved from the profile page to the Home
    overview page - see services.home_widgets.home_dashboard_context.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _priority_pins(self) -> list[Pin]:
        response = self.client.get(reverse("home.view"))
        return list(response.context["home_priority_unvisited_pins"])

    def test_unvisited_priority_pin_is_shown(self) -> None:
        pin = _make_pin(self.profile)
        self.assertIn(pin, self._priority_pins())

    def test_pin_with_last_visited_is_excluded(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        self.assertNotIn(pin, self._priority_pins())

    def test_pin_with_unsynced_visit_record_is_excluded(self) -> None:
        """A PinVisit exists but last_visited was never synced (e.g. a bulk import gap)."""
        pin = _make_pin(self.profile, last_visited=None)
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), source=VisitSource.HISTORY)
        self.assertNotIn(pin, self._priority_pins())


class RecentTripsQuerySetTests(TestCase):
    """Trip.objects.recently_active_past() - past trips with recent comment activity."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

    def _trip(self, *, start_date=None, end_date=None) -> Trip:
        trip = baker.make("dashboard.Trip", creator=self.profile, start_date=start_date, end_date=end_date)
        TripMembership.objects.create(trip=trip, profile=self.profile)
        return trip

    def _comment(self, trip: Trip, created: datetime.datetime) -> TripComment:
        comment = baker.make("dashboard.TripComment", trip=trip, author=self.profile, text="hi")
        TripComment.objects.filter(pk=comment.pk).update(created=created)
        return comment

    def _recent(self, days: int = 7) -> list[Trip]:
        since = timezone.now() - datetime.timedelta(days=days)
        return Trip.objects.recently_active_past(self.profile, since=since)

    def test_past_trip_with_recent_comment_is_included(self) -> None:
        today = timezone.localdate()
        trip = self._trip(start_date=today - datetime.timedelta(days=10), end_date=today - datetime.timedelta(days=8))
        self._comment(trip, timezone.now() - datetime.timedelta(days=1))
        self.assertIn(trip, self._recent())

    def test_past_trip_with_stale_comment_is_excluded(self) -> None:
        today = timezone.localdate()
        trip = self._trip(start_date=today - datetime.timedelta(days=30), end_date=today - datetime.timedelta(days=28))
        self._comment(trip, timezone.now() - datetime.timedelta(days=20))
        self.assertNotIn(trip, self._recent())

    def test_upcoming_trip_with_recent_comment_is_excluded(self) -> None:
        today = timezone.localdate()
        trip = self._trip(start_date=today + datetime.timedelta(days=5), end_date=today + datetime.timedelta(days=7))
        self._comment(trip, timezone.now() - datetime.timedelta(days=1))
        self.assertNotIn(trip, self._recent())

    def test_past_trip_with_no_comments_is_excluded(self) -> None:
        today = timezone.localdate()
        trip = self._trip(start_date=today - datetime.timedelta(days=10), end_date=today - datetime.timedelta(days=8))
        self.assertNotIn(trip, self._recent())

    def test_results_ordered_by_most_recent_comment_first(self) -> None:
        today = timezone.localdate()
        older_trip = self._trip(start_date=today - datetime.timedelta(days=10), end_date=today - datetime.timedelta(days=8))
        newer_trip = self._trip(start_date=today - datetime.timedelta(days=20), end_date=today - datetime.timedelta(days=18))
        self._comment(older_trip, timezone.now() - datetime.timedelta(days=5))
        self._comment(newer_trip, timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(self._recent(), [newer_trip, older_trip])
