"""The map toolbar's saved filters narrow the pin query in SQL, not through a list of pin uuids."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.maps import _apply_toolbar_filters
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter


class SavedFiltersComposeInSqlTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.alpha = SavedFilter.objects.create(profile=self.profile, name="Alpha", criteria={"name": "Alpha"})
        self.north = SavedFilter.objects.create(profile=self.profile, name="North", criteria={"name": "North"})

    def _pin(self, name: str) -> Pin:
        return baker.make(Pin, profile=self.profile, name=name, location=baker.make(Location))

    def _filtered(self, raw_ids: str):
        return _apply_toolbar_filters(Pin.objects.filter(profile=self.profile).root_pins(), self.profile, raw_ids)

    def test_every_active_filter_must_match(self) -> None:
        both = self._pin("Alpha North")
        self._pin("Alpha South")
        self._pin("Beta North")

        result = self._filtered(f"{self.alpha.uuid},{self.north.uuid}")

        self.assertEqual(list(result), [both])

    def test_building_the_filter_reads_no_pins(self) -> None:
        for index in range(5):
            self._pin(f"Alpha {index}")

        with CaptureQueriesContext(connection) as ctx:
            self._filtered(f"{self.alpha.uuid},{self.north.uuid}")

        self.assertFalse([q["sql"] for q in ctx.captured_queries if '"dashboard_user_pins"' in q["sql"]])

    def test_evaluating_is_one_query_with_no_uuid_parameters_whatever_the_pin_count(self) -> None:
        def evaluate() -> tuple[int, str]:
            query = self._filtered(f"{self.alpha.uuid},{self.north.uuid}")
            with CaptureQueriesContext(connection) as ctx:
                list(query)
            return len(ctx.captured_queries), ctx.captured_queries[-1]["sql"]

        self._pin("Alpha North 0")
        small_count, small_sql = evaluate()
        for index in range(1, 30):
            self._pin(f"Alpha North {index}")
        large_count, large_sql = evaluate()

        self.assertEqual(small_count, 1)
        self.assertEqual(large_count, 1)
        self.assertEqual(len(small_sql), len(large_sql))

    def test_malformed_and_foreign_ids_are_ignored(self) -> None:
        mine = self._pin("Alpha")
        stranger = baker.make(User).profile
        foreign = SavedFilter.objects.create(profile=stranger, name="Nothing", criteria={"name": "zzz-no-match"})

        result = self._filtered(f"zzz,,{foreign.uuid}")

        self.assertEqual(list(result), [mine])

    def test_malformed_id_on_the_pin_list_panel_is_not_a_server_error(self) -> None:
        self.client.force_login(self.user)

        response = self.client.get(reverse("map.pins.list"), {"toolbar_filter_ids": "zzz"})

        self.assertEqual(response.status_code, 200)
