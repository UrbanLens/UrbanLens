"""Tests for the trips list page's `sort`/`dir` query params (TripListView.get).

Invariants verified:
  - With no query params, trips are ordered by most-recently-updated first (existing
    default behavior, preserved by the new sort feature).
  - `?sort=updated&dir=asc` reverses that order.
  - `?sort=start_date&dir=asc`/`desc` orders by start date, and trips with no
    start_date always sort to the end regardless of direction.
  - Unrecognized `sort`/`dir` values fall back to the defaults instead of erroring.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripMembership

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

_LIST_URL = "/dashboard/trips/list/"


def _make_trip(creator_profile: Profile, **kwargs) -> Trip:
    trip = Trip.objects.create(name=kwargs.pop("name", "Test Trip"), creator=creator_profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=creator_profile, defaults={"rsvp": "yes"})
    return trip


class TripListSortTests(TestCase):
    user: User
    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _names(self, resp) -> list[str]:
        return [t.name for t in resp.context["trips"]]

    def test_default_sorts_by_updated_descending(self) -> None:
        older = _make_trip(self.profile, name="Older")
        newer = _make_trip(self.profile, name="Newer")
        Trip.objects.filter(pk=older.pk).update(updated=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
        Trip.objects.filter(pk=newer.pk).update(updated=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC))

        resp = self.client.get(_LIST_URL)

        self.assertEqual(self._names(resp), ["Newer", "Older"])
        self.assertEqual(resp.context["sort"], "updated")
        self.assertEqual(resp.context["dir"], "desc")

    def test_updated_ascending_reverses_order(self) -> None:
        older = _make_trip(self.profile, name="Older")
        newer = _make_trip(self.profile, name="Newer")
        Trip.objects.filter(pk=older.pk).update(updated=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
        Trip.objects.filter(pk=newer.pk).update(updated=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC))

        resp = self.client.get(_LIST_URL, {"sort": "updated", "dir": "asc"})

        self.assertEqual(self._names(resp), ["Older", "Newer"])

    def test_start_date_ascending_puts_soonest_first_and_undated_last(self) -> None:
        _make_trip(self.profile, name="No Date")
        _make_trip(self.profile, name="July", start_date=datetime.date(2026, 7, 1))
        _make_trip(self.profile, name="March", start_date=datetime.date(2026, 3, 1))

        resp = self.client.get(_LIST_URL, {"sort": "start_date", "dir": "asc"})

        self.assertEqual(self._names(resp), ["March", "July", "No Date"])

    def test_start_date_descending_puts_latest_first_and_undated_last(self) -> None:
        _make_trip(self.profile, name="No Date")
        _make_trip(self.profile, name="July", start_date=datetime.date(2026, 7, 1))
        _make_trip(self.profile, name="March", start_date=datetime.date(2026, 3, 1))

        resp = self.client.get(_LIST_URL, {"sort": "start_date", "dir": "desc"})

        self.assertEqual(self._names(resp), ["July", "March", "No Date"])

    def test_invalid_sort_and_dir_fall_back_to_defaults(self) -> None:
        resp = self.client.get(_LIST_URL, {"sort": "not-a-field", "dir": "sideways"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sort"], "updated")
        self.assertEqual(resp.context["dir"], "desc")
