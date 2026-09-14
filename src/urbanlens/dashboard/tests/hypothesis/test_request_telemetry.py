"""The slow-request log must fire on a slow request and stay quiet on a fast one."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from django.db import connection
from django.http import HttpResponse
from django.test import override_settings
from django.urls import path
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label

if TYPE_CHECKING:
    from django.http import HttpRequest

#: Long enough to clear any threshold set below it without slowing the suite.
SLOW_SECONDS = 0.25

#: Rows the querying view reads, so `sql_rows` has a known right answer.
SEEDED_LABELS = 7


def slow_view(request: HttpRequest) -> HttpResponse:
    """Burn wall time without touching the database."""
    time.sleep(SLOW_SECONDS)
    return HttpResponse("slow")


def fast_view(request: HttpRequest) -> HttpResponse:
    """Return immediately."""
    return HttpResponse("fast")


def querying_view(request: HttpRequest) -> HttpResponse:
    """Read a known number of rows, slowly enough to be reported."""
    rows = list(Label.objects.all().values_list("id", flat=True))
    time.sleep(SLOW_SECONDS)
    return HttpResponse(f"read {len(rows)}")


def raising_view(request: HttpRequest) -> HttpResponse:
    """Take a while and then fail, which is the case a naive version misses."""
    time.sleep(SLOW_SECONDS)
    raise RuntimeError("deliberate")


urlpatterns = [
    path("slow/", slow_view, name="telemetry.slow"),
    path("fast/", fast_view, name="telemetry.fast"),
    path("querying/", querying_view, name="telemetry.querying"),
    path("raising/", raising_view, name="telemetry.raising"),
]

_URLCONF = __name__


def _slow_lines(records: list) -> list[str]:
    """The formatted slow-request lines out of a caplog record list."""
    return [record.getMessage() for record in records if record.getMessage().startswith("slow request")]


def _field(line: str, name: str) -> str:
    """One `key=value` field out of a log line.

    Args:
        line: The formatted log line.
        name: The field to read.

    Returns:
        The field's value.

    Raises:
        AssertionError: The field is absent, which every assertion here depends on and none of them would otherwise notice."""
    match = re.search(rf"\b{re.escape(name)}=(\S+)", line)
    if match is None:
        raise AssertionError(f"no {name}= field in {line!r}")
    return match.group(1)


@override_settings(ROOT_URLCONF=_URLCONF, UL_SLOW_REQUEST_MS=100)
class TheThresholdDecidesTests(TestCase):
    """It must fire on slow, and only on slow."""

    def test_a_slow_request_is_logged(self) -> None:
        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            self.client.get("/slow/")

        lines = _slow_lines(logged.records)
        self.assertEqual(len(lines), 1, f"expected exactly one slow-request line, got {lines!r}")

    def test_a_fast_request_is_not_logged(self) -> None:
        """The half that a reversed comparison would break silently."""
        with self.assertNoLogs("urbanlens.dashboard.middleware", level="WARNING"):
            self.client.get("/fast/")

    @override_settings(UL_SLOW_REQUEST_MS=0)
    def test_a_zero_threshold_disables_it_rather_than_logging_everything(self) -> None:
        with self.assertNoLogs("urbanlens.dashboard.middleware", level="WARNING"):
            self.client.get("/slow/")


@override_settings(ROOT_URLCONF=_URLCONF, UL_SLOW_REQUEST_MS=100)
class TheNumbersAreRightTests(TestCase):
    """A line whose fields are wrong is worse than no line."""

    def test_wall_time_reflects_the_sleep(self) -> None:
        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            self.client.get("/slow/")

        wall_ms = float(_field(_slow_lines(logged.records)[0], "wall_ms"))
        self.assertGreaterEqual(wall_ms, SLOW_SECONDS * 1000 * 0.9)

    def test_cpu_time_separates_waiting_from_working(self) -> None:
        """The field R27 turned on, and the reason a total alone is not enough.

        `/slow/` sleeps, so its wall time is large and its CPU time is nearly nothing."""
        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            self.client.get("/slow/")

        line = _slow_lines(logged.records)[0]
        wall_ms, cpu_ms = float(_field(line, "wall_ms")), float(_field(line, "cpu_ms"))
        self.assertLess(
            cpu_ms,
            wall_ms / 2,
            f"a sleeping view reported cpu_ms={cpu_ms} against wall_ms={wall_ms}; the CPU clock is "
            "not measuring what it claims",
        )

    def test_rows_fetched_counts_the_rows_the_view_read(self) -> None:
        """`sql_rows` is what makes P107's shape visible in a log line."""
        for index in range(SEEDED_LABELS):
            Label.objects.create(kind="tag", name=f"Telemetry {index}")

        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            self.client.get("/querying/")

        line = _slow_lines(logged.records)[0]
        self.assertGreaterEqual(
            int(_field(line, "sql_rows")),
            SEEDED_LABELS,
            f"read {SEEDED_LABELS} labels but reported sql_rows={_field(line, 'sql_rows')}",
        )
        self.assertGreater(int(_field(line, "sql_n")), 0, "a view that queried reported no statements")

    def test_a_view_that_does_not_query_reports_no_sql(self) -> None:
        """The negative control for the SQL fields."""
        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            self.client.get("/slow/")

        line = _slow_lines(logged.records)[0]
        self.assertEqual(int(_field(line, "sql_n")), 0, f"a view that ran no query reported {line!r}")

    def test_the_line_names_the_view(self) -> None:
        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            self.client.get("/slow/")

        self.assertEqual(_field(_slow_lines(logged.records)[0], "view"), "telemetry.slow")


@override_settings(ROOT_URLCONF=_URLCONF, UL_SLOW_REQUEST_MS=100)
class ASlowRequestThatFailsIsStillReportedTests(TestCase):
    """A slow request that then failed is the one most worth having a line for.

    Measured rather than assumed, and the first draft of this asserted the wrong thing: Django wraps **every**
    middleware in `convert_exception_to_response`, so a view's exception has already become a 500 by the time it
    reaches this middleware on the way out."""

    def test_a_slow_request_that_five_hundreds_is_logged(self) -> None:
        with (
            self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged,
            pytest.raises(RuntimeError, match="deliberate"),
        ):
            self.client.get("/raising/")

        lines = _slow_lines(logged.records)
        self.assertEqual(len(lines), 1, f"expected a line for the failed request, got {lines!r}")
        self.assertEqual(_field(lines[0], "status"), "500")

    def test_a_request_with_no_response_reports_raised(self) -> None:
        """The `finally`'s other branch, exercised where it can be reached.

        `response=None` is what the middleware holds when the call it wrapped did not return one."""
        from urbanlens.dashboard.middleware import RequestTelemetryMiddleware, _SqlStats

        request = self.client.request().wsgi_request

        with self.assertLogs("urbanlens.dashboard.middleware", level="WARNING") as logged:
            RequestTelemetryMiddleware._log(request, None, 1234.0, 12.0, _SqlStats())

        line = _slow_lines(logged.records)[0]
        self.assertEqual(_field(line, "status"), "raised")
        self.assertEqual(_field(line, "bytes"), "-", "a request with no response has no byte count to report")


class TheMiddlewareIsActuallyInstalledTests(TestCase):
    """A middleware absent from the stack passes every test above that overrides it."""

    def test_it_is_in_the_middleware_setting(self) -> None:
        from django.conf import settings

        self.assertIn(
            "urbanlens.dashboard.middleware.RequestTelemetryMiddleware",
            settings.MIDDLEWARE,
        )

    def test_the_threshold_setting_exists_in_production_settings(self) -> None:
        """`override_settings` invents a name production lacks, so assert the real one.

        Every test above sets `UL_SLOW_REQUEST_MS`; without this, all of them
        would keep passing against a middleware reading a setting nobody defines.
        """
        from django.conf import settings

        self.assertTrue(hasattr(settings, "UL_SLOW_REQUEST_MS"))
        self.assertGreater(settings.UL_SLOW_REQUEST_MS, 0)

    def test_it_sits_below_authentication_so_it_can_name_the_user(self) -> None:
        from django.conf import settings

        stack = list(settings.MIDDLEWARE)
        self.assertLess(
            stack.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
            stack.index("urbanlens.dashboard.middleware.RequestTelemetryMiddleware"),
            "the telemetry middleware reports request.user, which does not exist above AuthenticationMiddleware",
        )


class TheSqlStatsAccumulatorTests(TestCase):
    """The rowcount rule the endpoint mixin also depends on."""

    def test_a_negative_rowcount_is_not_summed_as_zero(self) -> None:
        """psycopg reports -1 for "not determined", which is not none-fetched."""
        from urbanlens.dashboard.middleware import _SqlStats

        stats = _SqlStats()

        class _Cursor:
            rowcount = -1

        stats(lambda *args: None, "SELECT 1", None, False, {"cursor": _Cursor()})

        self.assertEqual(stats.rows, 0)
        self.assertEqual(stats.count, 1, "the statement itself should still be counted")

    def test_it_counts_a_statement_that_raised(self) -> None:
        """A failing query still cost time, and hiding it flatters the total."""
        from urbanlens.dashboard.middleware import _SqlStats

        stats = _SqlStats()

        def boom(*args: object) -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            stats(boom, "SELECT 1", None, False, {"cursor": None})

        self.assertEqual(stats.count, 1)


class TheWrapperDoesNotChangeBehaviourTests(TestCase):
    """Instrumentation must be transparent to the thing it measures."""

    def test_queries_still_return_their_rows(self) -> None:
        from urbanlens.dashboard.middleware import _SqlStats

        Label.objects.create(kind="tag", name="Transparent")

        with connection.execute_wrapper(_SqlStats()):
            names = list(Label.objects.filter(name="Transparent").values_list("name", flat=True))

        self.assertEqual(names, ["Transparent"])
