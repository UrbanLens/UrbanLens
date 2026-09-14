"""Assert that one more row does not cost an unreasonable amount of render time."""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

from urbanlens.core.tests.scaling import SeedScalingMixin

#: Timed repeats per data size, taking the minimum.
SAMPLES = 5

#: Rows before the first and second timed measurement. Smaller than the query
#: mixin's: each size is rendered ``SAMPLES + 1`` times, so the seed is paid for
#: repeatedly, and the signal is a per-row cost rather than a count.
FIRST_BATCH = 3
SECOND_BATCH = 9

#: What fraction of the page's own zero-row render one added row may cost.
MAX_ROW_COST_FRACTION = 0.10


@dataclass(frozen=True, slots=True)
class RenderSample:
    """One size's timing, kept whole so a failure can show its spread."""

    best_ms: float
    samples_ms: tuple[float, ...]
    body_bytes: int


class RenderTimeScalingMixin(SeedScalingMixin):
    """Mixin asserting a row's render cost is small next to the page's own.

    Subclasses implement :meth:`seed_rows`, exactly as for the query mixin.
    """

    first_batch: int = FIRST_BATCH
    second_batch: int = SECOND_BATCH

    #: Overridable for an endpoint whose seed is too slow to render six times.
    samples: int = SAMPLES

    def time_request(self, url: str, **extra: Any) -> RenderSample:
        """Fetch *url* repeatedly, returning the fastest render.

        One warm-up request is discarded first.

        Args:
            url: The URL to fetch. **extra: Passed to the test client.

        Returns:
            The best of :attr:`samples` timings, all of them, and the body size."""
        response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")

        timings: list[float] = []
        for _ in range(self.samples):
            started = time.perf_counter()
            response = self.client.get(url, **extra)
            timings.append((time.perf_counter() - started) * 1000)
            # A 302 or a 500 renders fast, and timing one would pass this test
            # while measuring nothing.
            self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")

        return RenderSample(best_ms=min(timings), samples_ms=tuple(timings), body_bytes=len(response.content))

    def query_count(self, url: str, **extra: Any) -> int:
        """How many queries *url* costs, for the failure message only.

        Deliberately a separate, untimed request: capturing queries turns on ``force_debug_cursor``, which would
        perturb every timing taken under it.

        Args:
            url: The URL to fetch. **extra: Passed to the test client.

        Returns:
            The number of statements executed."""
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url, **extra)
        return len(captured.captured_queries)

    def assert_row_cost_bounded(
        self,
        url: str,
        *,
        max_row_cost: float = MAX_ROW_COST_FRACTION,
        expect_growth: bool = True,
        growth_waiver: str = "",
        **extra: Any,
    ) -> None:
        """Assert one more row costs a small fraction of *url*'s empty render.

        Args:
            url: The URL to measure. max_row_cost: Fraction of the zero-row render one row may cost.

        Raises:
            AssertionError: A row costs more than *max_row_cost* of the page, or the seed did not exercise the endpoint."""
        # Before the measurement, not after it: this one costs a baseline
        # render, two seeds and four more requests, and the sibling mixin
        # refuses the same mistake immediately.
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")

        baseline = self.time_request(url, **extra)

        self.seed(self.first_batch)
        small = self.time_request(url, **extra)
        small_queries = self.query_count(url, **extra)

        self.seed(self.second_batch)
        large = self.time_request(url, **extra)
        large_queries = self.query_count(url, **extra)

        self.assert_seed_exercised_endpoint(
            url, small.body_bytes, large.body_bytes, expect_growth=expect_growth, growth_waiver=growth_waiver
        )

        row_bytes = (large.body_bytes - small.body_bytes) / self.second_batch
        row_ms = (large.best_ms - small.best_ms) / self.second_batch
        fraction = row_ms / baseline.best_ms
        if fraction <= max_row_cost:
            return

        total = self.first_batch + self.second_batch
        queries = f"{small_queries} -> {large_queries}"
        verdict = (
            "flat, so this is Python/template time, not the database"
            if large_queries <= small_queries
            else "also growing - check the query mixin first"
        )
        try:
            load = ", ".join(f"{value:.2f}" for value in os.getloadavg())
        except OSError:  # pragma: no cover - not every platform reports it
            load = "unavailable"

        raise AssertionError(
            f"{url} costs {row_ms:.1f} ms of render time per extra row - "
            f"{fraction:.0%} of the page's own {baseline.best_ms:.1f} ms empty render, over the {max_row_cost:.0%} budget.\n"
            f"    rows       0  {self.first_batch:>8}  {total:>8}      (best of {self.samples} requests each)\n"
            f"    render  {baseline.best_ms:6.1f}  {small.best_ms:8.1f}  {large.best_ms:8.1f}  ms\n"
            f"    body    {baseline.body_bytes:6d}  {small.body_bytes:8d}  {large.body_bytes:8d}  bytes -> {row_bytes:,.0f} per row\n"
            f"    queries {queries:>22}  -> {verdict}\n"
            f"    host load average: {load}\n"
            "A row this expensive is usually markup rendered whether or not anyone looks at it. "
            "Render it once per page, defer it to reveal, or paginate. If the cost is genuinely "
            "necessary, pass max_row_cost= with a comment saying why.",
        )
