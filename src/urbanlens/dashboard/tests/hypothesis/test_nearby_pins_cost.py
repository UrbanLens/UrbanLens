"""The Private Pin page's nearby layer costs the same queries for two neighbours as for twenty.

It serialised each neighbour with `to_detail_json`, whose name reads the location's wiki and whose icon reads the
pin's labels, one query each per pin: 266 queries a request on average under the capacity test.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.integration_testing.accounts import prepare_signed_in_account


class NearbyPinsCostTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = prepare_signed_in_account(self.user)
        self.client.force_login(self.user)
        self.url = reverse("pin.nearby_pins.json", kwargs={"pin_slug": self._pin(0, name="Mill").ensure_slug()})
        self.client.get(self.url)  # A first request of the day also records the login.

    def _pin(self, index: int, *, name: str = "") -> Pin:
        location = baker.make(Location, latitude=41.0 + index / 10_000, longitude=-71.0)
        pin = baker.make(Pin, profile=self.profile, name=name, location=location)
        pin.labels.add(baker.make(Label, profile=self.profile, kind="tag"))
        return pin

    def _measure(self) -> tuple[int, int]:
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries), len(response.json()["pins"])

    def test_the_query_count_does_not_grow_with_the_neighbours(self) -> None:
        for index in range(1, 3):
            self._pin(index)
        few, few_pins = self._measure()
        for index in range(3, 21):
            self._pin(index)
        many, many_pins = self._measure()

        self.assertEqual((few_pins, many_pins), (2, 20))
        self.assertEqual(many, few)
