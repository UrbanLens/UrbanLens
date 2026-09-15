"""Asking to confirm a bulk add to a list reads a count, not every pin the filter matched.

The view loaded each match as a model before deciding whether to ask, so a filter matching an account's twenty
thousand pins fetched twenty thousand rows to answer "add 20,000 pins?".
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.middleware import _SqlStats
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList


class BulkAddCostTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_list = PinList.objects.create(profile=self.profile, name="Mills")
        self.url = reverse("lists.items.add", kwargs={"list_slug": self.pin_list.slug})

    def _ask(self) -> tuple[int, int]:
        stats = _SqlStats()
        with connection.execute_wrapper(stats):
            response = self.client.post(self.url, {"name": ""})
        self.assertEqual(response.status_code, 409)
        return response.json()["count"], stats.rows

    def test_asking_reads_no_more_rows_for_more_matches(self) -> None:
        baker.make(Pin, profile=self.profile, _quantity=101)
        few, few_rows = self._ask()
        baker.make(Pin, profile=self.profile, _quantity=200)
        many, many_rows = self._ask()

        self.assertEqual((few, many), (101, 301))
        self.assertEqual(many_rows, few_rows)

    def test_pins_already_on_the_list_are_not_counted(self) -> None:
        pins = baker.make(Pin, profile=self.profile, _quantity=105)
        self.client.post(self.url, {"pin_ids": [pin.pk for pin in pins[:3]]})

        count, _ = self._ask()

        self.assertEqual(count, 102)

    def test_confirming_adds_every_match_after_those_already_there(self) -> None:
        pins = baker.make(Pin, profile=self.profile, _quantity=101)
        self.client.post(self.url, {"pin_ids": [pins[0].pk]})

        response = self.client.post(self.url, {"name": "", "confirmed": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.pin_list.items.values_list("pin_id", flat=True)), {pin.pk for pin in pins})
        self.assertEqual(self.pin_list.items.filter(order=0).get().pin_id, pins[0].pk)
