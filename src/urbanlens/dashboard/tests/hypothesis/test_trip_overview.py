"""Tests for the trips overview page (TripOverviewView) and view-tracking.

Invariants verified:
  - GET /trips/ renders the overview page and computes stat-tile counts by
    timeline_status.
  - "Recently updated" is ordered by Trip.updated, independent of viewing.
  - "Recently viewed" only includes trips the viewer has opened, ordered by
    their own TripMembership.last_viewed_at (not another member's).
  - Visiting a trip's detail page (TripDetailView) stamps the viewer's own
    membership row's last_viewed_at, without touching other members' rows.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripMembership

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def _make_trip(creator_profile: Profile, **kwargs) -> Trip:
    trip = Trip.objects.create(name=kwargs.pop("name", "Test Trip"), creator=creator_profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=creator_profile, defaults={"rsvp": "yes"})
    return trip


class TripOverviewViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client_ = Client()
        self.client_.force_login(self.user)

    def _url(self) -> str:
        return reverse("trips.overview")

    def test_renders_for_authenticated_user_with_no_trips(self) -> None:
        resp = self.client_.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["stats"]["total"], 0)

    def test_stats_count_by_timeline_status(self) -> None:
        today = timezone.now().date()
        _make_trip(self.profile, name="Upcoming", start_date=today + datetime.timedelta(days=5))
        _make_trip(self.profile, name="Past", start_date=today - datetime.timedelta(days=10), end_date=today - datetime.timedelta(days=8))
        _make_trip(self.profile, name="Planning")

        resp = self.client_.get(self._url())

        stats = resp.context["stats"]
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["upcoming"], 1)
        self.assertEqual(stats["past"], 1)
        self.assertEqual(stats["planning"], 1)

    def test_recently_updated_ordered_by_updated_descending(self) -> None:
        older = _make_trip(self.profile, name="Older")
        newer = _make_trip(self.profile, name="Newer")
        Trip.objects.filter(pk=older.pk).update(updated=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
        Trip.objects.filter(pk=newer.pk).update(updated=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC))

        resp = self.client_.get(self._url())

        names = [t.name for t in resp.context["recently_updated_trips"]]
        self.assertEqual(names, ["Newer", "Older"])

    def test_recently_viewed_excludes_never_opened_trips(self) -> None:
        _make_trip(self.profile, name="Never opened")

        resp = self.client_.get(self._url())

        self.assertEqual(list(resp.context["recently_viewed_trips"]), [])

    def test_recently_viewed_reflects_viewer_own_membership_only(self) -> None:
        trip = _make_trip(self.profile, name="Shared Trip")
        other_user = baker.make("auth.User")
        other_profile = other_user.profile
        TripMembership.objects.create(trip=trip, profile=other_profile, last_viewed_at=timezone.now())

        resp = self.client_.get(self._url())

        # The viewer never opened the trip, even though the other member did.
        self.assertEqual(list(resp.context["recently_viewed_trips"]), [])


class TripDetailViewLastViewedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def test_viewing_trip_stamps_viewer_membership(self) -> None:
        client = Client()
        client.force_login(self.member_user)

        membership = TripMembership.objects.get(trip=self.trip, profile=self.member)
        self.assertIsNone(membership.last_viewed_at)

        client.get(reverse("trips.detail", kwargs={"trip_slug": self.trip.slug}))

        membership.refresh_from_db()
        self.assertIsNotNone(membership.last_viewed_at)

    def test_viewing_trip_does_not_stamp_other_members(self) -> None:
        client = Client()
        client.force_login(self.member_user)

        client.get(reverse("trips.detail", kwargs={"trip_slug": self.trip.slug}))

        creator_membership = TripMembership.objects.get(trip=self.trip, profile=self.creator)
        self.assertIsNone(creator_membership.last_viewed_at)
