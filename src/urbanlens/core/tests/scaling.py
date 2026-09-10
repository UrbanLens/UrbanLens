"""What a scaling test needs whatever it is measuring.

Two mixins measure how an endpoint behaves as its data grows -
:class:`~urbanlens.core.tests.query_scaling.QueryScalingMixin` counts queries,
:class:`~urbanlens.core.tests.render_scaling.RenderTimeScalingMixin` times the
render - and both need the same two things: a way to create more of the rows
the endpoint lists, and a guard that the seed actually changed what it renders.

The guard is the part worth sharing. A scaling test whose seed grows something
the endpoint does not list renders the same page twice and passes without
measuring anything, and that is not hypothetical: during the 2026-08-17 audit a
survey reported the conversation list flat while seeding pins, and seeding
conversations properly found about eleven queries per row.
"""

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

#: How many more bytes the response must return before the seed counts as having
#: exercised the endpoint. A response is not perfectly stable between identical
#: requests - recorded activity, streak counters and timestamps move it a little -
#: so "grew at all" is too weak a test. Measured on ``trips.overview``: four
#: repeats with nothing seeded spanned **11 bytes**, while ten real trips added
#: **12,033**. Three orders of magnitude apart, so the floor only has to sit
#: clear of the noise.
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

        Seeding through the ORM leaves ``pg_class.reltuples`` and ``pg_statistic``
        describing the table as it was before - usually empty - so the planner
        chooses for a table that no longer exists. That is not a small effect
        and it looks exactly like the regression a scaling test is hunting:
        measuring ``MapPinPayloadService.all()`` at 5,000 pins read **4.683s**
        without this and **0.384s** with it (N10,
        ``docs/notes/map-perf-measurement-and-test-gaps.md``), and the session
        that hit it spent three rounds blaming its own change.

        ``ANALYZE`` is legal inside a transaction, and ``pg_statistic`` is an
        ordinary table, so the refresh rolls back with the test like everything
        else.

        Args:
            count: How many rows to add.
        """
        with CaptureQueriesContext(connection) as captured:
            self.seed_rows(count)
        written = self._written_tables(query["sql"] for query in captured.captured_queries)
        self.analyze(written | {name.lower() for name in self.extra_analyzed_tables})

    @staticmethod
    def _written_tables(statements: Iterable[str]) -> set[str]:
        """The tables a batch of statements wrote to.

        Derived from the seed's own SQL rather than declared per test class:
        the set that needs re-analysing is exactly the set that was written, a
        test cannot forget to update it, and it stays right when a signal
        writes somewhere the test never mentions.

        Args:
            statements: The executed statements.

        Returns:
            Table names, lowercased.
        """
        written: set[str] = set()
        for sql in statements:
            match = _WRITE_TARGET.search(sql)
            if match:
                written.add(match.group(1).lower())
        return written

    def analyze(self, tables: set[str]) -> None:
        """Refresh planner statistics for exactly *tables*.

        Takes the complete set rather than deriving any of it, so a caller - or
        a test watching this - sees the same list the database does.
        :meth:`seed` is where the policy of what to include lives.

        Deliberately never a bare ``ANALYZE``. Measured against this schema's
        237 tables: whole-database ``ANALYZE`` costs 3.55s cold and 1.70s warm,
        against 45ms for three named tables - and a scaling assertion seeds
        twice, so the bare form would add several seconds to every one of them.

        Args:
            tables: Tables to analyse. Empty analyses nothing.
        """
        targets = sorted(tables)
        if not targets or connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            # Identifiers, so they are quoted rather than parameterised. Every
            # name here came from Django's own generated SQL, not from a test's
            # input, but quote them anyway so this cannot become an injection
            # site if that ever stops being true.
            quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in targets)
            cursor.execute(f"ANALYZE {quoted}")

    def seed_rows(self, count: int) -> None:
        """Create *count* more of whatever the endpoint under test lists.

        The rows created here must be the rows the endpoint renders. Seeding
        something else produces a constant-size response and a meaningless pass,
        which is what ``expect_growth`` guards against.

        Args:
            count: How many rows to add.
        """
        raise NotImplementedError("scaling tests must seed the rows their endpoint lists")

    def assert_seed_exercised_endpoint(
        self, url: str, small_body: int, large_body: int, *, expect_growth: bool, growth_waiver: str
    ) -> None:
        """Fail unless the seed changed what *url* renders.

        Args:
            url: The URL that was measured, for the message.
            small_body: Response length at ``first_batch`` rows.
            large_body: Response length at ``first_batch + second_batch`` rows.
            expect_growth: Require the body to have grown. Turn this off only
                for endpoints that cap what they render (pagination).
            growth_waiver: Why this endpoint's response cannot grow. Required
                when *expect_growth* is False, so the exemption is legible.

        Raises:
            AssertionError: The seed did not exercise the endpoint, or growth
                was waived without a reason.
        """
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
