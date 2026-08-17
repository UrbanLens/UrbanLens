"""List endpoints must cost a constant number of queries regardless of row count.

Each of these was a real N+1 found by rendering the same endpoint at two data
sizes and diffing the query count. They are easy to reintroduce: every one came
from a model *property* that quietly falls back to a query (``Location.display_name``
reading its wiki, ``Trip.effective_start_date`` aggregating its activities,
``Profile.username`` reading its user), so adding an innocuous-looking field to a
template or payload is enough to bring one back.

Asserting "does not grow" rather than an exact count on purpose: an exact number
turns every unrelated query change into a failing test, and the thing worth
protecting is the *slope*, not the intercept.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

#: Rows to add before the first and second measurement. The second must be large
#: enough that a single per-row query is unmistakable against normal variation.
_FIRST_BATCH = 2
_SECOND_BATCH = 10


class QueryScalingTests(TestCase):
    """Rendering more rows must not mean running more queries."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        _key, self.raw_key = generate_api_key(self.user, "scaling")
        api_key = self.user.api_keys.first()
        api_key.scopes = list(ApiKeyScope.values)
        api_key.save()
        self.client.force_login(self.user)

    def _seed(self, count: int) -> None:
        for _ in range(count):
            location = baker.make(Location)
            pin = baker.make(Pin, profile=self.profile, location=location)
            pin.labels.add(baker.make(Label, profile=self.profile, kind="tag"))
            baker.make(Image, pin=pin, profile=self.profile, location=location)
            trip = baker.make(Trip, creator=self.profile)
            trip.profiles.add(self.profile)

    def _count(self, url: str, **extra) -> int:
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
        return len(ctx.captured_queries)

    def _assert_flat(self, url: str, **extra) -> None:
        self._seed(_FIRST_BATCH)
        small = self._count(url, **extra)
        self._seed(_SECOND_BATCH)
        large = self._count(url, **extra)
        self.assertLessEqual(
            large,
            small + 2,
            f"{url} ran {small} queries for {_FIRST_BATCH} rows and {large} for "
            f"{_FIRST_BATCH + _SECOND_BATCH} - it is querying per row.",
        )

    def test_map_pin_list_panel_does_not_scale_with_pin_count(self) -> None:
        self._assert_flat(reverse("map.pins.list"))

    def test_trips_overview_does_not_scale_with_trip_count(self) -> None:
        self._assert_flat(reverse("trips.overview"))

    def test_trips_calendar_does_not_scale_with_trip_count(self) -> None:
        self._assert_flat(reverse("trips.calendar"))

    def test_external_photo_list_does_not_scale_with_photo_count(self) -> None:
        self._assert_flat(reverse("external_api:photos"), HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")

    # The three below were surveyed rather than found broken: each was measured at two
    # data sizes, came out flat, and is pinned here so it stays that way. The seed
    # above grows pins, labels, images and trips, which is what these list - an
    # endpoint whose row type the seed does not grow would render a constant-size
    # list and pass without measuring anything. (``memories.photos`` was measured flat
    # too, but needs images with real files rather than the bare rows seeded here, so
    # pinning it would mean changing the seed under the four tests above.)
    def test_trips_list_does_not_scale_with_trip_count(self) -> None:
        self._assert_flat(reverse("trips.list"))

    def test_label_index_does_not_scale_with_label_count(self) -> None:
        self._assert_flat(reverse("label.index", kwargs={"label_kind": "tags"}))

    def test_organize_index_does_not_scale_with_pin_count(self) -> None:
        self._assert_flat(reverse("organize.index"))

