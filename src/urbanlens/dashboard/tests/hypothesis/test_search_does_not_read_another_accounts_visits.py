"""Searching your own visits must not read somebody else's visit log.

:class:`VisitSearchProvider` scopes by ``pin__profile=profile`` - a join, so which table Postgres
starts from is its choice, not the scope's. At capacity scale it starts from the visit table:
a sequential scan of all 16,208 visits hash-joined to the viewer's pins, 15.40 ms whose cost is the
site's visit count. Every other provider's access scope was bounded by
``test_search_does_not_read_another_accounts_*``; this one had no file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits import PinVisit
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import VisitSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through a visit note on the viewer's own pin.
TERM = "quokka"

#: A word the viewer never searches for, for visits that must be scanned to be rejected.
OTHER = "wombat"

#: Visits the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's visits were read.
TOLERANCE = 20


class _VisitCase(TestCase):
    """A viewer with one logged visit, and a stranger logging visits to a pin of their own."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.visited = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.my_pin = baker.make(Pin, profile=self.viewer, location=my_location, name="My Pin", description="")
        baker.make(PinVisit, pin=self.my_pin, notes=f"{TERM} mine")

        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_pin = baker.make(
            Pin, profile=self.stranger, location=their_location, name="Their Pin", description=""
        )
        self.seed_strangers_visits(FIRST_BATCH)

    def seed_strangers_visits(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's pin *count* more logged visits, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.visited += 1
            baker.make(PinVisit, pin=self.their_pin, notes=f"{word} theirs {self.visited}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {PinVisit._meta.db_table}, {Pin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's bare-term visit search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = VisitSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def visit_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the visit table."""
        _, captured = self.search()
        table = PinVisit._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.visit_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the visit table, so nothing was measured")
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_visits(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            captured, statements = self.visit_reading_statements()
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's visit search read {before} rows, then {after} after a stranger logged {count} "
                f"{kind} visits to a pin of their own - {after - before} more rows for a pin the viewer cannot "
                f"see. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsVisitsTests(_VisitCase):
    """One account's visit log must not be read to answer a different account's visit search."""

    def test_it_does_not_read_a_strangers_matching_visits(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_visits(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_VisitCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_visit(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            len(results),
            1,
            "the viewer's search did not return their own logged visit, so the measured statement is the empty path",
        )

    def test_the_strangers_matching_visits_match_the_term(self) -> None:
        self.seed_strangers_visits(SECOND_BATCH)
        matching = PinVisit.objects.filter(pin=self.their_pin, notes__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded visits do not match the search term"
        )

    def test_the_strangers_unrelated_visits_do_not_match_the_term(self) -> None:
        self.seed_strangers_visits(SECOND_BATCH, matching=False)
        matching = PinVisit.objects.filter(pin=self.their_pin, notes__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_pin_is_not_visible_to_the_viewer(self) -> None:
        self.assertEqual(Pin.objects.filter(profile=self.viewer, pk=self.their_pin.pk).count(), 0)

    def test_the_measured_statement_reads_the_visit_table(self) -> None:
        captured, statements = self.visit_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the visit table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            PinVisit._meta.db_table, touched, f"no plan read the visit table; relations read were {sorted(touched)}"
        )
