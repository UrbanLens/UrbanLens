"""Assert that an endpoint's query count does not grow with its row count."""

from __future__ import annotations

import collections
import re
from typing import TYPE_CHECKING, Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

from urbanlens.core.tests.scaling import FIRST_BATCH, MIN_GROWTH_BYTES, SECOND_BATCH, SeedScalingMixin

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Re-exported so a caller importing them from here keeps resolving.
__all__ = [
    "FIRST_BATCH",
    "MIN_GROWTH_BYTES",
    "SECOND_BATCH",
    "QueryScalingMixin",
    "normalize_sql",
    "queries_that_grew",
]

#: Queries may legitimately differ by a couple between runs (a count query that
#: only appears once there is a second page, say). Anything above this is slope.
DEFAULT_TOLERANCE = 2

_DIGITS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'")


def normalize_sql(sql: str, *, width: int = 130) -> str:
    """Reduce a statement to its shape, so repeated executions collapse together.

    Args:
        sql: The executed statement.
        width: How much of the normalised statement to keep for reporting.

    Returns:
        The statement with digits and quoted literals replaced, truncated."""
    return _QUOTED.sub("'X'", _DIGITS.sub("N", sql))[:width]


def queries_that_grew(before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Which normalised statements ran more times in *after* than in *before*.

    Args:
        before: Captured queries from the smaller data set.
        after: Captured queries from the larger one.

    Returns:
        ``(before_count, after_count, sql)`` triples, most-grown first."""
    small = collections.Counter(normalize_sql(query["sql"]) for query in before)
    large = collections.Counter(normalize_sql(query["sql"]) for query in after)
    grown = [(small.get(sql, 0), count, sql) for sql, count in large.items() if count > small.get(sql, 0)]
    return sorted(grown, key=lambda row: row[1] - row[0], reverse=True)


class QueryScalingMixin(SeedScalingMixin):
    """Mixin for Django ``TestCase``s asserting an endpoint's query count is flat.

    Subclasses implement :meth:`seed_rows`; everything else is provided.
    """

    def measure(self, url: str, **extra: Any) -> tuple[list[dict[str, Any]], int]:
        """Fetch *url*, returning its captured queries and response body length.

        Args:
            url: The URL to fetch. **extra: Passed to the test client (headers, auth).

        Returns:
            The captured queries and the response body's length in bytes."""
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
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
            url: The URL to measure. tolerance: Extra queries allowed at the larger size before this is treated as slope rather than noise....

        Raises:
            AssertionError: The endpoint queries per row, or the seed did not change what it renders."""
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")

        self.seed(self.first_batch)
        small_queries, small_body = self.measure(url, **extra)
        self.seed(self.second_batch)
        large_queries, large_body = self.measure(url, **extra)

        total = self.first_batch + self.second_batch
        self.assert_seed_exercised_endpoint(
            url, small_body, large_body, expect_growth=expect_growth, growth_waiver=growth_waiver
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
