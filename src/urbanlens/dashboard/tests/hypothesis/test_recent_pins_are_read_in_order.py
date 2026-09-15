"""Home's recent pins are read newest-first from an index, not sorted out of the whole account.

The widget shows six pins. With no index on (profile, created) Postgres read and sorted every pin the account had to
find them: 17,720 rows of up to 2 KB each for the capacity population's heaviest account, spilling to disk, 27-64 ms
on every home page load.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.home.home_widgets import home_dashboard_context


def _node_types(plan: dict) -> list[str]:
    found = [plan["Node Type"]]
    for child in plan.get("Plans", []):
        found.extend(_node_types(child))
    return found


class RecentPinsAreReadInOrderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.profile = baker.make(User).profile

    def test_recent_pins_need_no_sort(self) -> None:
        sql, params = home_dashboard_context(self.profile)["home_recent_pins"].query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute("SET enable_sort = off")
            try:
                cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}", params)
                row = cursor.fetchone()
            finally:
                cursor.execute("RESET enable_sort")
        self.assertIsNotNone(row)
        raw = row[0] if row else "[]"
        plan = raw if isinstance(raw, list) else json.loads(raw)

        self.assertNotIn("Sort", _node_types(plan[0]["Plan"]))
