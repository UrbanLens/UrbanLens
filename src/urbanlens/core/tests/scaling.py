"""What a scaling test needs whatever it is measuring."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.db import connection
from django.test.utils import CaptureQueriesContext

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.core.tests.testcase import TestCase

    # Spelled this way so mypy sees the real assertion API inside the mixin
    # bodies while the runtime MRO stays a plain mixin over whatever TestCase
    # the subclass names.
    _Base = TestCase
else:
    _Base = object

#: Rows seeded before the first and second measurement. The second is large
#: enough that one query per row is unmistakable against normal variation.
FIRST_BATCH = 2
SECOND_BATCH = 10

#: How many more bytes the response must return before the seed counts as having exercised the endpoint.
MIN_GROWTH_BYTES = 200

#: Statements that leave a table's planner statistics describing a table that no
#: longer exists. ``INSERT`` is the one that matters for a seed; the other two are
#: here because a seed that rewrites or prunes rows invalidates the same estimates.
_WRITE_TARGET = re.compile(
    r'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"?([a-zA-Z_][a-zA-Z0-9_$]*)"?',
    re.IGNORECASE,
)


class SeedScalingMixin(_Base):
    """The seed contract, and the guard that the seed did something."""

    #: Overridable per test class - a slow seed may want smaller batches.
    first_batch: int = FIRST_BATCH
    second_batch: int = SECOND_BATCH

    #: Tables to refresh statistics for beyond the ones the seed wrote to.
    #: Only needed when something *else* changes the row counts a measured query
    #: plans against - a trigger, or rows created by a signal through raw SQL.
    extra_analyzed_tables: tuple[str, ...] = ()

    def seed(self, count: int) -> None:
        """Create *count* rows, then make the planner aware they exist.

        Args:
            count: How many rows to add."""
        with CaptureQueriesContext(connection) as captured:
            self.seed_rows(count)
        written = self._written_tables(query["sql"] for query in captured.captured_queries)
        self.analyze(written | {name.lower() for name in self.extra_analyzed_tables})

    @staticmethod
    def _written_tables(statements: Iterable[str]) -> set[str]:
        """The tables a batch of statements wrote to.

        Derived from the seed's own SQL rather than declared per test class: the set that needs re-analysing is
        exactly the set that was written, a test cannot forget to update it, and it stays right when a signal
        writes somewhere the test never mentions.

        Args:
            statements: The executed statements.

        Returns:
            Table names, lowercased."""
        written: set[str] = set()
        for sql in statements:
            match = _WRITE_TARGET.search(sql)
            if match:
                written.add(match.group(1).lower())
        return written

    def analyze(self, tables: set[str]) -> None:
        """Refresh planner statistics for exactly *tables*.

        Takes the complete set rather than deriving any of it, so a caller - or a test watching this - sees the
        same list the database does. :meth:`seed` is where the policy of what to include lives.

        Args:
            tables: Tables to analyse."""
        targets = sorted(tables)
        if not targets or connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in targets)
            cursor.execute(f"ANALYZE {quoted}")

    def seed_rows(self, count: int) -> None:
        """Create *count* more of whatever the endpoint under test lists.

        The rows created here must be the rows the endpoint renders.

        Args:
            count: How many rows to add."""
        raise NotImplementedError("scaling tests must seed the rows their endpoint lists")

    def assert_seed_exercised_endpoint(
        self, url: str, small_body: int, large_body: int, *, expect_growth: bool, growth_waiver: str
    ) -> None:
        """Fail unless the seed changed what *url* renders.

        Args:
            url: The URL that was measured, for the message. small_body: Response length at ``first_batch`` rows. large_body: Response length at...

        Raises:
            AssertionError: The seed did not exercise the endpoint, or growth was waived without a reason."""
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")
        if not expect_growth:
            return

        total = self.first_batch + self.second_batch
        self.assertGreaterEqual(
            large_body - small_body,
            MIN_GROWTH_BYTES,
            f"{url} returned {small_body} bytes for {self.first_batch} rows and {large_body} for {total} - "
            f"a change of {large_body - small_body}, under the {MIN_GROWTH_BYTES}-byte noise floor. "
            "The seed does not exercise this endpoint, so a flat measurement would prove nothing. "
            "Seed the rows this endpoint actually lists, or pass expect_growth=False with a reason.",
        )
