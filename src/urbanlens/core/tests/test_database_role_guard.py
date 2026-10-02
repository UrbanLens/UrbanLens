"""A run that cannot create its test database says where tests do run, instead of failing in CREATE DATABASE (P130)."""

from __future__ import annotations

from typing import Self

from django.db import OperationalError

from urbanlens.core.tests.database_role_guard import refusal_for_role
from urbanlens.core.tests.testcase import SimpleTestCase


class _Cursor:
    def __init__(self, row: tuple[str, bool] | Exception) -> None:
        self.row = row

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _sql: str) -> None:
        if isinstance(self.row, Exception):
            raise self.row

    def fetchone(self) -> tuple[str, bool]:
        assert not isinstance(self.row, Exception)
        return self.row


class _Connection:
    def __init__(self, row: tuple[str, bool] | Exception) -> None:
        self.row = row

    def cursor(self) -> _Cursor:
        return _Cursor(self.row)


class RefusalForRoleTests(SimpleTestCase):
    def test_a_role_that_cannot_create_databases_is_sent_to_the_test_runner(self) -> None:
        refusal = refusal_for_role(_Connection(("ul_web", False)))

        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertIn("ul_web", refusal)
        self.assertIn("bin/run_tests.sh", refusal)

    def test_a_role_that_can_create_databases_runs(self) -> None:
        self.assertIsNone(refusal_for_role(_Connection(("postgres", True))))

    def test_an_unreachable_database_is_left_to_fail_as_it_would_have(self) -> None:
        self.assertIsNone(refusal_for_role(_Connection(OperationalError("could not connect"))))
