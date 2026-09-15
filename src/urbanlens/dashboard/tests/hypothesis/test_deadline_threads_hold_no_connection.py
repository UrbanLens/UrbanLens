"""A deadline thread must not keep a database connection the web tier's budget never counted.

``ul_web``'s limit is sized to the request threads, each keeping its connection. ``call_with_deadline`` runs on a
64-thread pool of its own; if a call there that touched the ORM left its connection open, every pool thread that
ever ran one would hold a backend for ``CONN_MAX_AGE`` on top of the request threads.
"""

from __future__ import annotations

from unittest import mock

from django.db import connection, connections

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.core.timeout_utils import call_with_deadline


class ADeadlineThreadClosesWhatItOpenedTests(TestCase):
    def _query_on_the_pool(self) -> dict:
        seen: dict = {}

        def _query() -> bool:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            seen["wrapper"] = connections["default"]
            seen["opened"] = connections["default"].connection is not None
            return True

        with mock.patch.dict(connections["default"].settings_dict, {"CONN_MAX_AGE": 300}):
            self.assertTrue(call_with_deadline(_query, timeout=10, default=False))
        return seen

    def test_a_call_that_queried_leaves_no_connection_behind(self) -> None:
        seen = self._query_on_the_pool()

        self.assertTrue(seen["opened"], "the call never reached the database, so nothing here was tested")
        self.assertIsNone(seen["wrapper"].connection, "a persistent connection outlived the call on a deadline thread")

    def test_the_request_threads_own_connection_is_left_alone(self) -> None:
        """Anti-vacuity: closing every thread's connections would pass the test above and break the request."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

        self._query_on_the_pool()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))
