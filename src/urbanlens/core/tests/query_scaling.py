"""Assert that an endpoint's query count does not grow with its row count.

Three things this provides that hand-written scaling tests kept getting wrong:

**The measurement has to be exercised.** A scaling test whose seed grows pins
while the endpoint lists conversations renders the same list twice and passes
without measuring anything. That happened during the 2026-08-17 audit: a survey
reported the conversation list "flat" at both sizes, and seeding conversations
properly revealed about eleven queries per row. So :meth:`assert_flat` requires
the response body to *grow* between the two measurements, and fails with "the
seed does not exercise this endpoint" when it doesn't - a wrong test now fails
loudly instead of passing quietly.

**A failure should say what grew.** Diffing the two runs' SQL by hand took a
separate diagnostic run every time. On failure, :meth:`assert_flat` normalises
the captured statements (digits and quoted literals replaced) and reports which
ones multiplied, so the cause is in the failure message rather than a session
away.

**Row count is not always body length.** Paginated endpoints cap what they
render, so their body stops growing while the query count still can. Those pass
``expect_growth=False`` with a reason, which is a deliberate, visible decision
rather than a silent one.
"""

from __future__ import annotations

import collections
import re
from typing import TYPE_CHECKING, Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Rows seeded before the first and second measurement. The second is large
#: enough that one query per row is unmistakable against normal variation.
FIRST_BATCH = 2
SECOND_BATCH = 10

#: Queries may legitimately differ by a couple between runs (a count query that
#: only appears once there is a second page, say). Anything above this is slope.
DEFAULT_TOLERANCE = 2

#: How many more bytes the response must return before the seed counts as having
#: exercised the endpoint. A response is not perfectly stable between identical
#: requests - recorded activity, streak counters and timestamps move it a little -
#: so "grew at all" is too weak a test. Measured on ``trips.overview``: four
#: repeats with nothing seeded spanned **11 bytes**, while ten real trips added
#: **12,033**. Three orders of magnitude apart, so the floor only has to sit
#: clear of the noise.
MIN_GROWTH_BYTES = 200

_DIGITS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'")


def normalize_sql(sql: str, *, width: int = 130) -> str:
    """Reduce a statement to its shape, so repeated executions collapse together.

    Args:
        sql: The executed statement.
        width: How much of the normalised statement to keep for reporting.

    Returns:
        The statement with digits and quoted literals replaced, truncated.
    """
    return _QUOTED.sub("'X'", _DIGITS.sub("N", sql))[:width]


def queries_that_grew(before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Which normalised statements ran more times in *after* than in *before*.

    Args:
        before: Captured queries from the smaller data set.
        after: Captured queries from the larger one.

    Returns:
        ``(before_count, after_count, sql)`` triples, most-grown first.
    """
    small = collections.Counter(normalize_sql(query["sql"]) for query in before)
    large = collections.Counter(normalize_sql(query["sql"]) for query in after)
    grown = [(small.get(sql, 0), count, sql) for sql, count in large.items() if count > small.get(sql, 0)]
    return sorted(grown, key=lambda row: row[1] - row[0], reverse=True)


class QueryScalingMixin:
    """Mixin for Django ``TestCase``s asserting an endpoint's query count is flat.

    Subclasses implement :meth:`seed_rows`; everything else is provided.
    """

    #: Overridable per test class - a slow seed may want smaller batches.
    first_batch: int = FIRST_BATCH
    second_batch: int = SECOND_BATCH

    def seed_rows(self, count: int) -> None:
        """Create *count* more of whatever the endpoint under test lists.

        The rows created here must be the rows the endpoint renders. Seeding
        something else produces a constant-size response and a meaningless pass,
        which is what ``expect_growth`` guards against.

        Args:
            count: How many rows to add.
        """
        raise NotImplementedError("scaling tests must seed the rows their endpoint lists")

    def measure(self, url: str, **extra: Any) -> tuple[list[dict[str, Any]], int]:
        """Fetch *url*, returning its captured queries and response body length.

        Args:
            url: The URL to fetch.
            **extra: Passed to the test client (headers, auth).

        Returns:
            The captured queries and the response body's length in bytes.
        """
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, **extra)  # type: ignore[attr-defined]
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")  # type: ignore[attr-defined]
        return list(captured.captured_queries), len(response.content)

    def assert_flat(
        self,
        url: str,
        *,
        tolerance: int = DEFAULT_TOLERANCE,
        expect_growth: bool = True,
        growth_waiver: str = "",
        **extra: Any,
    ) -> None:
        """Assert *url* costs the same number of queries at two data sizes.

        Args:
            url: The URL to measure.
            tolerance: Extra queries allowed at the larger size before this is
                treated as slope rather than noise.
            expect_growth: Require the response body to grow between the two
                measurements. Turn this off only for endpoints that cap what
                they render (pagination), and say why in *growth_waiver*.
            growth_waiver: Why this endpoint's response cannot grow. Required
                when *expect_growth* is False, so the exemption is legible.
            **extra: Passed to the test client.

        Raises:
            AssertionError: The endpoint queries per row, or the seed did not
                change what it renders.
        """
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")

        self.seed_rows(self.first_batch)
        small_queries, small_body = self.measure(url, **extra)
        self.seed_rows(self.second_batch)
        large_queries, large_body = self.measure(url, **extra)

        total = self.first_batch + self.second_batch
        if expect_growth:
            self.assertGreaterEqual(  # type: ignore[attr-defined]
                large_body - small_body,
                MIN_GROWTH_BYTES,
                f"{url} returned {small_body} bytes for {self.first_batch} rows and {large_body} for {total} - "
                f"a change of {large_body - small_body}, under the {MIN_GROWTH_BYTES}-byte noise floor. "
                "The seed does not exercise this endpoint, so a flat query count would prove nothing. "
                "Seed the rows this endpoint actually lists, or pass expect_growth=False with a reason.",
            )

        if large_queries and len(large_queries) > len(small_queries) + tolerance:
            report = "\n".join(
                f"      {before:3d} -> {after:3d}  {sql}"
                for before, after, sql in queries_that_grew(small_queries, large_queries)[:5]
            )
            raise AssertionError(
                f"{url} ran {len(small_queries)} queries for {self.first_batch} rows and "
                f"{len(large_queries)} for {total} - it is querying per row.\n"
                f"    queries that grew:\n{report}",
            )
