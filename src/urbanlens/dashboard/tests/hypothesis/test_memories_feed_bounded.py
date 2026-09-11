"""The memories feed must be bounded by the server, not by the client's honesty.

`MemoriesFeedDataView`'s own docstring says "a date range is always applied
(defaulting to the trailing 90 days) so a profile's full history is never loaded
in a single request". That was not true: `_parse_date` falls back to the default
only when a value fails to *parse*, and `0001-01-01` parses fine. So "All time"
asked for all time and the aggregator built a `MemoryEvent` for every route,
trip, visit and geotagged photo the account had ever recorded, in one request.

Capping needs the sources ordered first. Each one yielded in whatever order
Postgres returned, so taking the first N of an unordered source keeps an
arbitrary N, not the newest N - a cap that silently returns the wrong events is
worse than no cap. The first test here is that ordering, because every other
assertion depends on it.

Pagination rather than a clamped window, because "All time" should keep meaning
all time: the response says how far it got, and the client asks for the next
page ending there.
"""

from __future__ import annotations

import datetime
import json

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.memories.aggregator import get_memory_events


class _FeedCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude="40.0", longitude="-75.0")
        )
        self.today = timezone.now().date()

    def seed_visits(self, count: int) -> list[datetime.date]:
        """One visit a day, going backwards from today. Returns their dates."""
        dates = []
        for day in range(count):
            when = timezone.now() - datetime.timedelta(days=day)
            baker.make(PinVisit, pin=self.pin, visited_at=when)
            dates.append(when.date())
        return dates


class TheSourcesYieldNewestFirstTests(_FeedCase):
    """Without this, a cap keeps an arbitrary subset rather than the newest."""

    def test_visits_come_back_newest_first(self) -> None:
        self.seed_visits(8)
        events = get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today)
        visits = [event for event in events if event.type == "visit"]

        self.assertEqual(len(visits), 8)
        stamps = [event.occurred_at for event in visits]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "visits were not ordered newest-first")


class TheAggregatorRespectsALimitTests(_FeedCase):
    def test_no_more_than_the_limit_comes_back(self) -> None:
        self.seed_visits(12)
        events = get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today, limit=5)
        self.assertEqual(len(events), 5)

    def test_the_events_kept_are_the_newest_ones(self) -> None:
        """The property a cap over unordered sources would silently break."""
        self.seed_visits(12)
        events = get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today, limit=3)

        returned = {event.occurred_at.date() for event in events}
        self.assertEqual(
            returned, {self.today, self.today - datetime.timedelta(days=1), self.today - datetime.timedelta(days=2)}
        )

    def test_without_a_limit_everything_in_range_comes_back(self) -> None:
        """Non-vacuity: the limit is doing the bounding, not the range."""
        self.seed_visits(12)
        events = get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today)
        self.assertEqual(len([e for e in events if e.type == "visit"]), 12)


class TheFeedEndpointIsBoundedTests(_FeedCase):
    def _feed(self, **params: str) -> dict:
        response = self.client.get(reverse("memories.data"), params)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_an_all_time_request_does_not_load_all_time(self) -> None:
        """The exact request the "All time" button sends."""
        self.seed_visits(30)
        from urbanlens.dashboard.controllers.memories import MAX_FEED_EVENTS

        body = self._feed(start="0001-01-01", end="9999-12-31")
        self.assertLessEqual(len(body["events"]), MAX_FEED_EVENTS)

    def test_a_truncated_feed_says_so_and_says_where_to_continue(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.controllers import memories

        self.seed_visits(10)
        with mock.patch.object(memories, "MAX_FEED_EVENTS", 4):
            body = self._feed(start="0001-01-01", end="9999-12-31")

        self.assertEqual(len(body["events"]), 4)
        self.assertTrue(body["truncated"], "the feed dropped events without admitting it")
        self.assertTrue(body["next_before"], "a truncated feed gave the client no way to ask for the next page")

    def test_an_untruncated_feed_offers_no_cursor(self) -> None:
        self.seed_visits(3)
        body = self._feed()

        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_before"])

    def test_a_single_day_holding_more_than_a_page_still_advances(self) -> None:
        """The reason the cursor is a timestamp and not a date. With a date
        cursor, a day with more events than the cap returns the same page
        forever, because the cursor can never move past that day."""
        from unittest import mock

        from urbanlens.dashboard.controllers import memories

        # Distinct times within one day, which is what a day of visits is.
        # Exactly-equal timestamps are a different, documented limitation.
        midday = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        for minute in range(9):
            baker.make(PinVisit, pin=self.pin, visited_at=midday - datetime.timedelta(minutes=minute))

        with mock.patch.object(memories, "MAX_FEED_EVENTS", 3):
            first = self._feed(start="0001-01-01", end="9999-12-31")
            second = self._feed(start="0001-01-01", end="9999-12-31", before=first["next_before"])

        self.assertEqual(len(first["events"]), 3)
        self.assertEqual(len(second["events"]), 3)
        first_stamps = {event["occurred_at"] for event in first["events"]}
        second_stamps = {event["occurred_at"] for event in second["events"]}
        self.assertFalse(first_stamps & second_stamps, "the page did not advance within a single day")

    def test_the_cursor_returns_the_next_page_and_does_not_repeat(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.controllers import memories

        self.seed_visits(10)
        with mock.patch.object(memories, "MAX_FEED_EVENTS", 4):
            first = self._feed(start="0001-01-01", end="9999-12-31")
            second = self._feed(start="0001-01-01", end="9999-12-31", before=first["next_before"])

        first_stamps = {event["occurred_at"] for event in first["events"]}
        second_stamps = {event["occurred_at"] for event in second["events"]}
        self.assertTrue(second_stamps, "the second page was empty")
        self.assertFalse(first_stamps & second_stamps, "the second page repeated events from the first")
