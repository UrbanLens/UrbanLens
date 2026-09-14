"""Every axis a collection endpoint can grow along, measured in one seed pass."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from django.db import connection
from django.http import HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.test.utils import CaptureQueriesContext

from urbanlens.core.tests.instantiation_scaling import InstantiationScalingMixin, count_instantiations
from urbanlens.core.tests.query_scaling import DEFAULT_TOLERANCE, queries_that_grew

#: Model instances one row of output may cost. A projection path builds none; a
#: DRF view with nested serializers legitimately builds some and should say how
#: many, and why, at its own call site.
MAX_OBJECTS_PER_ROW = 1.0

#: Database rows one *rendered* row may cost, for an endpoint whose output grows
#: with the table. One row read per row shown is the healthy case; the headroom
#: is for a join or a count query alongside it.
MAX_ROWS_FETCHED_PER_ROW = 1.5

#: The same budget for an endpoint whose output does **not** grow - a paginated list, or one returning a count.
#: Much stricter, because that is the whole assertion: if the response is capped, the reading behind it must be
#: capped too.
MAX_ROWS_FETCHED_PER_ROW_WHEN_CAPPED = 0.5

#: Response bytes one row may add. Generous: this catches an endpoint shipping a
#: whole serialized graph per row, not one with a verbose field.
MAX_BYTES_PER_ROW = 4_000


def read_body(response: HttpResponseBase) -> bytes:
    """The whole body, whether or not it was streamed.

    `StreamingHttpResponse` raises on `.content`, so reading a body the obvious way put `map.document` - the
    largest endpoint in the application - outside the reach of every gate built on this mixin.

    Args:
        response: The response to read.

    Returns:
        The body as bytes.

    Raises:
        TypeError: The response carries a body in neither of the two shapes Django defines, or it streams asynchronously, which cannot be drained from a..."""
    if isinstance(response, StreamingHttpResponse):
        chunks = response.streaming_content
        if not isinstance(chunks, Iterator):
            raise TypeError("an async streamed response cannot be drained synchronously")
        body = b"".join(chunks)
        response.streaming_content = iter([body])
        return body
    if isinstance(response, HttpResponse):
        return bytes(response.content)
    raise TypeError(f"cannot read a body from {type(response).__name__}")


@dataclass(frozen=True, slots=True)
class EndpointSample:
    """One measurement of an endpoint, on every axis at once."""

    queries: tuple[dict[str, Any], ...]
    objects: int
    by_model: dict[str, int] = field(default_factory=dict)
    rows_fetched: int = 0
    body_bytes: int = 0
    payload_rows: int | None = None


def _row_counting_wrapper(totals: list[int]) -> Any:
    """A `connection.execute_wrapper` that sums the rows each statement returned.

    Args:
        totals: Single-element accumulator, mutated in place.

    Returns:
        The wrapper callable."""

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: dict[str, Any]) -> Any:
        result = execute(sql, params, many, context)
        cursor = context.get("cursor")
        # psycopg2 sets rowcount after execute for SELECT as well as for writes.
        # -1 means "not determined", which is not the same as zero and must not
        # be summed as if it were.
        count = getattr(cursor, "rowcount", -1)
        if isinstance(count, int) and count > 0:
            totals[0] += count
        return result

    return wrapper


class EndpointScalingMixin(InstantiationScalingMixin):
    """Assert an endpoint's cost per row is bounded on every axis at once.

    Subclasses implement :meth:`seed_rows` and call :meth:`assert_endpoint_scaling`."""

    def request(self, url: str, **extra: Any) -> HttpResponse:
        """Issue the request under test. Override for POST or a body.

        Args:
            url: The URL to fetch. **extra: Passed to the test client.

        Returns:
            The response."""
        return self.client.get(url, **extra)

    def count_payload_rows(self, response: HttpResponse) -> int | None:
        """How many records the response carries, if that is countable.

        Returns None by default, which skips the ceiling axis.

        Args:
            response: The response to read.

        Returns:
            The record count, or None to skip."""
        return None

    def measure_endpoint(self, url: str, **extra: Any) -> EndpointSample:
        """Fetch *url* once, reading every axis off the same request.

        Args:
            url: The URL to measure. **extra: Passed to the test client.

        Returns:
            The sample."""
        rows: list[int] = [0]
        with connection.execute_wrapper(_row_counting_wrapper(rows)), count_instantiations() as counted:
            response = self.request(url, **extra)
            # Drained inside the wrappers, not after them: a streamed response has
            # done none of its work yet at this point, so reading it outside would
            # report zero rows and zero objects for the endpoint that builds the
            # most of both.
            body = read_body(response)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
        return EndpointSample(
            queries=(),
            objects=counted.total,
            by_model=dict(counted.by_model),
            rows_fetched=rows[0],
            body_bytes=len(body),
            payload_rows=self.count_payload_rows(response),
        )

    def measure_queries(self, url: str, **extra: Any) -> tuple[dict[str, Any], ...]:
        """The statements *url* runs, captured separately from the timed axes.

        `CaptureQueriesContext` turns on ``force_debug_cursor``, which is fine for counting but is not something
        to hold while measuring anything else.

        Args:
            url: The URL to measure. **extra: Passed to the test client.

        Returns:
            The captured queries."""
        with CaptureQueriesContext(connection) as captured:
            self.request(url, **extra)
        return tuple(captured.captured_queries)

    def assert_endpoint_scaling(
        self,
        url: str,
        *,
        max_objects_per_row: float = MAX_OBJECTS_PER_ROW,
        max_rows_fetched_per_row: float | None = None,
        max_bytes_per_row: int = MAX_BYTES_PER_ROW,
        query_tolerance: int = DEFAULT_TOLERANCE,
        payload_ceiling: int | None = None,
        expect_growth: bool = True,
        growth_waiver: str = "",
        **extra: Any,
    ) -> None:
        """Assert every per-row cost of *url* is bounded, reporting all failures.

        Args:
            url: The URL to measure. max_objects_per_row: Model instances one row may cost. max_rows_fetched_per_row: Database rows one row may cost.

        Raises:
            AssertionError: Any axis grew past its budget, or the seed did not exercise the endpoint."""
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")

        self.seed(self.first_batch)
        small = self.measure_endpoint(url, **extra)
        small_queries = self.measure_queries(url, **extra)

        self.seed(self.second_batch)
        large = self.measure_endpoint(url, **extra)
        large_queries = self.measure_queries(url, **extra)

        self.assert_seed_exercised_endpoint(
            url,
            small.body_bytes,
            large.body_bytes,
            expect_growth=expect_growth,
            growth_waiver=growth_waiver,
        )

        if max_rows_fetched_per_row is None:
            max_rows_fetched_per_row = (
                MAX_ROWS_FETCHED_PER_ROW if expect_growth else MAX_ROWS_FETCHED_PER_ROW_WHEN_CAPPED
            )

        failures = list(
            self._failures(
                url=url,
                small=small,
                large=large,
                small_queries=small_queries,
                large_queries=large_queries,
                max_objects_per_row=max_objects_per_row,
                max_rows_fetched_per_row=max_rows_fetched_per_row,
                max_bytes_per_row=max_bytes_per_row,
                query_tolerance=query_tolerance,
                payload_ceiling=payload_ceiling,
            ),
        )
        if failures:
            total = self.first_batch + self.second_batch
            raise AssertionError(
                f"{url} costs too much per row, measured between {self.first_batch} and {total} rows:\n"
                + "\n".join(f"  - {failure}" for failure in failures),
            )

    def _failures(
        self,
        *,
        url: str,
        small: EndpointSample,
        large: EndpointSample,
        small_queries: tuple[dict[str, Any], ...],
        large_queries: tuple[dict[str, Any], ...],
        max_objects_per_row: float,
        max_rows_fetched_per_row: float,
        max_bytes_per_row: int,
        query_tolerance: int,
        payload_ceiling: int | None,
    ) -> Iterator[str]:
        """Every axis that grew past its budget, described.

        Yielded rather than raised one at a time so a failure reports the whole picture: an endpoint building
        objects per row usually also fetches rows per row, and fixing one at a time means measuring three times."""
        added = self.second_batch

        per_row = (large.objects - small.objects) / added
        if per_row > max_objects_per_row:
            breakdown = ", ".join(
                f"{name} {large.by_model[name] - small.by_model.get(name, 0):+d}"
                for name in sorted(large.by_model)
                if large.by_model[name] - small.by_model.get(name, 0) > 0
            )
            yield (
                f"objects/row {per_row:.1f} over {max_objects_per_row} "
                f"({small.objects} -> {large.objects}; {breakdown or 'no per-model growth'}) - "
                "a query counter reads this as flat"
            )

        rows_per_row = (large.rows_fetched - small.rows_fetched) / added
        if rows_per_row > max_rows_fetched_per_row:
            yield (
                f"rows fetched/row {rows_per_row:.1f} over {max_rows_fetched_per_row} "
                f"({small.rows_fetched} -> {large.rows_fetched}) - the view reads rows it does not "
                "render, which no statement count can see"
            )

        bytes_per_row = (large.body_bytes - small.body_bytes) / added
        if bytes_per_row > max_bytes_per_row:
            yield f"bytes/row {bytes_per_row:,.0f} over {max_bytes_per_row:,} ({small.body_bytes:,} -> {large.body_bytes:,})"

        if large_queries and len(large_queries) > len(small_queries) + query_tolerance:
            grew = queries_that_grew(small_queries, large_queries)[:3]
            report = "; ".join(f"{before}->{after} {sql[:70]}" for before, after, sql in grew)
            yield f"queries {len(small_queries)} -> {len(large_queries)} - querying per row ({report})"

        if payload_ceiling is not None:
            if large.payload_rows is None:
                yield (
                    f"payload_ceiling={payload_ceiling} was requested but count_payload_rows returned None, "
                    "so the ceiling was never checked"
                )
            elif large.payload_rows > payload_ceiling:
                yield (
                    f"payload carries {large.payload_rows} records against a ceiling of {payload_ceiling} - "
                    "the response size is set by how many rows the requester owns"
                )
