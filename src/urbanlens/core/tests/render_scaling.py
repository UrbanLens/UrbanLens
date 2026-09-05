"""Assert that one more row does not cost an unreasonable amount of render time.

`QueryScalingMixin` answers how *many* queries a row costs. It cannot see the
defect this exists for: the Organize Labels page had already been cut from ~146
queries to 3 and still took about twelve seconds to render 500 labels, because
the view rendered all six of its client-side tabs on every load. Query count was
flat and perfect the whole time.

**A growth ratio cannot see it either**, which is why this is not the shape the
problem entry originally prescribed. Render time on a list page is *supposed* to
be linear in rows, so a page whose rows are 60x too expensive still grows 4x when
the rows grow 4x - indistinguishable from a healthy page under any tolerance
worth setting. Measured over 25 trials at load 11.4 on 8 cores, the pathological
workload's 3-to-12-row ratio was 2.8-3.2, and so was everything else's.

**What separates the classes is the marginal cost of one row, expressed as a
fraction of the page's own zero-row render.** For ``T(n) = C + k*n``, taking
``(T(large) - T(small)) / (large - small)`` cancels ``C`` exactly - the client,
middleware, auth, base template, connection setup, all of it - and dividing by
``T(0)`` then cancels machine speed and steady load, because both terms scale
together. What is left is dimensionless and reads as a sentence: *one more row
costs X% of what the whole empty page costs.* A row costing 10% of an entire page
is a bug on sight, whatever hardware you are on.

Three design decisions worth not re-deriving:

- **Best-of-K, not the mean.** Contention only ever adds time, so the minimum is
  the estimate that converges. The same 25 trials put a benign row at 0.3-2.8% of
  baseline best-of-5, and at -5.6-6.3% by mean - the mean crosses zero where the
  minimum does not.
- **No superlinearity assertion.** A second-derivative check was designed and
  rejected on measurement: the slope ratio reached 3.24 on a *linear* workload and
  3.37 on an O(n^2) one whose constant was too small to matter, against 1.11-1.45
  on the genuinely broken one. At these sizes it is noise.
- **Database time is included, not subtracted.** Django rounds each statement's
  duration to the millisecond, so a page of 25 sub-millisecond queries carries
  more quantization error than the signal it would correct; reading those numbers
  at all requires ``force_debug_cursor``, which perturbs what it measures; and the
  baseline already subtracts everything constant. Query *count* is the sibling
  mixin's job, and the two are only interpretable as a pair - so a failure here
  reports the count rather than correcting for it.

**The one way to hold this wrong: seed the rows in ``setUp``.** The baseline is
the denominator, and rows already on the page at that point inflate it without
bound - measured, a page costing 4.8 baselines per row when empty reads as 0.05
with a dozen rows already rendered, which is inside any budget worth setting. It
cannot be detected from the outside, and the arithmetic says why: with a baseline
taken at ``n0`` rows the measurements give ``k`` and ``C + k*n0``, and no
combination of them separates ``C`` from ``n0``. So every row the endpoint lists
has to be created in :meth:`seed_rows` and nowhere else.
"""

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
#: Measured 2026-09-05 through the real request path, in the app container: a
#: two-span row came in at 0.011-0.040 across five trials, and a row rendering a
#: 1,249-button icon grid at 4.82. Sits 2.5x above the worst benign observation
#: and two orders of magnitude below the offending one.
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

        One warm-up request is discarded first. Django wraps its template
        loaders in the cached loader unconditionally, so template compilation,
        the staticfiles manifest, the URL resolver and any lazy imports in the
        view are one-time process costs that would otherwise land entirely on
        whichever measurement ran first.

        Args:
            url: The URL to fetch.
            **extra: Passed to the test client.

        Returns:
            The best of :attr:`samples` timings, all of them, and the body size.
        """
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

        Deliberately a separate, untimed request: capturing queries turns on
        ``force_debug_cursor``, which would perturb every timing taken under it.

        Args:
            url: The URL to fetch.
            **extra: Passed to the test client.

        Returns:
            The number of statements executed.
        """
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
            url: The URL to measure.
            max_row_cost: Fraction of the zero-row render one row may cost.
                Raise it only with a comment saying why the cost is necessary.
            expect_growth: Require the response body to grow between the two
                sizes. Turn this off only for endpoints that cap what they
                render, and say why in *growth_waiver*.
            growth_waiver: Why this endpoint's response cannot grow.
            **extra: Passed to the test client.

        Raises:
            AssertionError: A row costs more than *max_row_cost* of the page, or
                the seed did not exercise the endpoint.
        """
        baseline = self.time_request(url, **extra)

        self.seed_rows(self.first_batch)
        small = self.time_request(url, **extra)
        small_queries = self.query_count(url, **extra)

        self.seed_rows(self.second_batch)
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
