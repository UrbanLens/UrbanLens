"""Remove the ledger rows a test's deadline-pool threads committed, which the test's own transaction cannot roll back.

A view that calls an upstream through ``services.core.timeout_utils`` runs the call on a pool thread, and that thread
writes the call's ``ApiCallLog`` reservation (and creates the service's ``ApiRateLimit`` row) on its own database
connection, in autocommit. A ``TestCase`` rolls back only its own connection, so those rows outlive the test and every
later test in the process sees them: a test that walks every route left 93 of them behind, and a later test asserting
that a refused call wrote no ledger row failed once pytest-xdist happened to run it second.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from django.db import DEFAULT_DB_ALIAS, connections

if TYPE_CHECKING:
    from django.db.models import Model


def _ledger_models() -> tuple[type[Model], ...]:
    from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
    from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit

    return (ApiCallLog, ApiRateLimit)


def _on_a_separate_connection(statements: list[tuple[str, list[Any]]]) -> list[Any]:
    """Run each statement on a fresh autocommit connection, which sees only committed rows, and return the first column
    of each one's first row (``None`` for a statement that returns nothing)."""
    connection = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        results = []
        with connection.cursor() as cursor:
            for sql, params in statements:
                cursor.execute(sql, params)
                row = cursor.fetchone() if cursor.description else None
                results.append(row[0] if row else None)
        return results
    finally:
        connection.close()


class DiscardsLedgerRowsFromOtherThreadsMixin:
    """For a ``TestCase`` class whose requests reach the deadline pool: delete, once the class is done, every ledger
    row committed while it ran.

    Mixed in before ``TestCase``. Only rows above each table's highest id at class setup are touched, and only once the
    class's own transaction has rolled back, so nothing the migrations or another class committed is lost.
    """

    _ledger_high_water: ClassVar[dict[str, int]]

    @classmethod
    def setUpClass(cls) -> None:
        tables = [model._meta.db_table for model in _ledger_models()]
        marks = _on_a_separate_connection([(f'SELECT coalesce(max(id), 0) FROM "{table}"', []) for table in tables])
        cls._ledger_high_water = dict(zip(tables, marks, strict=True))
        super().setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _on_a_separate_connection(
            [(f'DELETE FROM "{table}" WHERE id > %s', [mark]) for table, mark in cls._ledger_high_water.items()]
        )
