"""Prepared statements, and the two settings that are both needed to get them.

X27 measured planning at 77.3% of database time for the short SELECTs that are 96% of web
traffic - 0.306ms planning against 0.090ms execution per call - and pgbench replaying the four
most-called queries at 1,306 tps simple against 3,298 tps prepared. Preparing them is the single
largest database win available, and it is bought entirely in configuration.

The trap is that the configuration has two halves and the missing half is silent. psycopg3 will
only prepare a statement whose parameters were bound server-side, so ``server_side_binding`` is
required; and Django then overrides psycopg's own default with ``None`` - preparation off - so
that a transaction-pooling proxy keeps working (``postgresql/base.py``, ``get_connection_params``).
Set one without the other and everything still works, exactly as fast as before.
"""

from __future__ import annotations

import os
from unittest import mock

from django.conf import settings
from django.db import connection
from django.db.backends.postgresql.psycopg_any import is_psycopg3

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.UrbanLens.settings._env import DEFAULT_PREPARE_THRESHOLD, prepare_threshold


class TheThresholdIsReadFromTheEnvironmentTests(SimpleTestCase):
    """``UL_DB_PREPARE_THRESHOLD`` exists so a deployment that grows a transaction-pooling proxy
    can turn preparation off without a release: a pooler hands a session to whoever asks next, and
    a statement prepared on one session is not there for the next."""

    def test_an_unset_variable_prepares(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(prepare_threshold(), DEFAULT_PREPARE_THRESHOLD)

    def test_a_threshold_is_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"UL_DB_PREPARE_THRESHOLD": "12"}):
            self.assertEqual(prepare_threshold(), 12)

    def test_zero_prepares_on_first_use(self) -> None:
        """psycopg reads 0 as "prepare immediately", which is a real choice and not "off"."""
        with mock.patch.dict(os.environ, {"UL_DB_PREPARE_THRESHOLD": "0"}):
            self.assertEqual(prepare_threshold(), 0)

    def test_a_pooler_can_turn_preparation_off(self) -> None:
        for raw in ("none", "off", "", "  None  "):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"UL_DB_PREPARE_THRESHOLD": raw}):
                self.assertIsNone(prepare_threshold())

    def test_an_unreadable_value_is_not_an_import_error(self) -> None:
        """Evaluated while settings are being built, where raising takes the process with it."""
        with mock.patch.dict(os.environ, {"UL_DB_PREPARE_THRESHOLD": "five"}):
            self.assertEqual(prepare_threshold(), DEFAULT_PREPARE_THRESHOLD)


class BothHalvesOfTheConfigurationAreWiredTests(SimpleTestCase):
    """Either half alone is a no-op that looks like a change."""

    def setUp(self) -> None:
        super().setUp()
        self.options = settings.DATABASES["default"]["OPTIONS"]

    def test_the_driver_is_psycopg3(self) -> None:
        """psycopg2 has no server-side binding and no ``prepare_threshold``; under it both keys
        below are settings the backend would reject rather than honour."""
        self.assertTrue(is_psycopg3)

    def test_parameters_are_bound_server_side(self) -> None:
        self.assertIs(self.options.get("server_side_binding"), True)

    def test_the_threshold_is_set_past_djangos_off(self) -> None:
        self.assertIsInstance(self.options.get("prepare_threshold"), int)
        self.assertGreaterEqual(self.options["prepare_threshold"], 0)

    def test_the_setting_is_wired_to_the_helper(self) -> None:
        """A helper nothing reads would pass every test in the class above."""
        self.assertEqual(self.options["prepare_threshold"], prepare_threshold())


class TheServerActuallyPreparesTests(TestCase):
    """The settings above are psycopg's to interpret, and a release of it could reinterpret them.
    Ask the server what it holds rather than trusting the dict."""

    #: Comfortably past ``DEFAULT_PREPARE_THRESHOLD`` without depending on its value.
    EXECUTIONS = DEFAULT_PREPARE_THRESHOLD + 3

    def _prepared_statements(self) -> set[str]:
        with connection.cursor() as cursor:
            cursor.execute("SELECT statement FROM pg_prepared_statements")
            return {row[0] for row in cursor.fetchall()}

    def test_a_repeated_query_ends_up_prepared(self) -> None:
        before = self._prepared_statements()
        with connection.cursor() as cursor:
            for value in range(self.EXECUTIONS):
                cursor.execute("SELECT %s::int + 1", [value])
                cursor.fetchone()

        self.assertIn("SELECT $1::int + 1", self._prepared_statements() - before)

    def test_one_execution_is_not_prepared(self) -> None:
        """Anti-vacuity: a driver that prepared everything would pass the test above whatever the
        threshold said."""
        if (settings.DATABASES["default"]["OPTIONS"]["prepare_threshold"] or 0) < 2:
            self.skipTest("this deployment prepares on first use, so there is nothing to distinguish")
        before = self._prepared_statements()
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s::int + 2", [1])
            cursor.fetchone()

        self.assertNotIn("SELECT $1::int + 2", self._prepared_statements() - before)
