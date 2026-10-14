"""Tests for the test database name read from ``UL_TEST_DB_NAME``."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from urbanlens.UrbanLens.settings._env import isolated_test_database_name


class TestDatabaseNameTests(SimpleTestCase):
    def test_a_name_without_the_prefix_gets_it(self) -> None:
        with mock.patch.dict("os.environ", {"UL_TEST_DB_NAME": "ul_fast"}):
            self.assertEqual(isolated_test_database_name(), "test_ul_fast")

    def test_a_prefixed_name_is_kept(self) -> None:
        with mock.patch.dict("os.environ", {"UL_TEST_DB_NAME": "test_run_a"}):
            self.assertEqual(isolated_test_database_name(), "test_run_a")

    def test_unset_or_blank_leaves_django_to_name_it(self) -> None:
        for env in ({}, {"UL_TEST_DB_NAME": ""}, {"UL_TEST_DB_NAME": "  "}):
            with mock.patch.dict("os.environ", env, clear=True), self.subTest(env=env):
                self.assertIsNone(isolated_test_database_name())
