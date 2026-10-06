"""A tallied service is held to its limits on the shared cache's counters, and its calls still reach the ledger.

``basemap_vendor_tiles`` is the service these were written for: one call per uncached tile, which on the per-call
ledger cost a locked ``ApiRateLimit`` row and an ``ApiCallLog`` insert each. Every call here goes through the real
rate-limited session; only the request underneath it is stubbed (``vendor_tile_wire``).
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from datetime import UTC, datetime
import time
from unittest import mock

from django.db import DatabaseError, connection
from django.db.models import Sum
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
import requests

from urbanlens.core.cache_backend import CacheUnavailableError
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.apis.locations.basemap_vendor_tiles_gateway import BasemapVendorTilesGateway
from urbanlens.dashboard.services.core import call_tally, counters, provider_health
from urbanlens.dashboard.services.core.counters import CounterUnavailableError
from urbanlens.dashboard.services.core.rate_limiter import (
    SERVICE_REGISTRY,
    CallLedger,
    EnvironmentRefusedError,
    RateLimiterUnavailableError,
    RateLimitExceededError,
    ServiceDisabledError,
    check_rate_limit,
    get_limit_config,
)
from urbanlens.dashboard.tests.hypothesis.vendor_tile_wire import vendor_answer, vendor_wire

_SERVICE = "basemap_vendor_tiles"
#: The middle of the minute this module was loaded in: recent enough for the 30-day summaries, and far enough
#: from the minute's edges that a test moving the clock a few seconds stays inside it.
_MINUTE_START = int(time.time() // 60 * 60)
_NOW = _MINUTE_START + 30


@contextlib.contextmanager
def deployment(
    environment: str, *, share: float | None = None, overrides: dict[str, float] | None = None
) -> Iterator[None]:
    """Pretend to be one deployment: its ``UL_ENVIRONMENT``, ``UL_ENVIRONMENT_SHARE`` and overrides (D26)."""
    with (
        override_settings(ENVIRONMENT_NAME=environment, TESTING=False),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share", share),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share_overrides", overrides or {}),
    ):
        yield


@contextlib.contextmanager
def clock(at: float) -> Iterator[None]:
    """Move the counters' windows to *at*, in seconds since the epoch."""
    with mock.patch.object(call_tally, "_now", return_value=at):
        yield


class _TalliedTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        get_limit_config(_SERVICE)

    def limits(self, **fields: object) -> None:
        """Change the service's row the way the API-limits page would, and stop this process remembering the old one."""
        ApiRateLimit.objects.filter(service=_SERVICE).update(**fields)
        call_tally.forget_limits()

    @staticmethod
    def fetch(layer: str = "satellite", x: int = 1) -> tuple[int, bytes, str]:
        return BasemapVendorTilesGateway().download_tile(layer, 5, x, 1)


class TheTileServiceIsTalliedTests(SimpleTestCase):
    def test_the_vendor_tile_service_is_declared_tallied(self) -> None:
        self.assertIs(SERVICE_REGISTRY[_SERVICE].ledger, CallLedger.TALLIED)

    def test_no_tallied_service_carries_a_budget_that_is_money(self) -> None:
        """The counters' windows are fixed, so a thirty-day budget can reach twice its limit across a boundary: fine
        for politeness, not for a bill."""
        for name, defaults in SERVICE_REGISTRY.items():
            if defaults.ledger is CallLedger.TALLIED:
                with self.subTest(service=name):
                    self.assertIsNone(defaults.calls_per_30_days)
                    self.assertIsNone(defaults.free_tier_per_calendar_month)
                    self.assertIsNone(defaults.cost_per_call)


class TheCountersHoldTheLimitTests(_TalliedTestCase):
    def test_a_call_past_the_per_minute_limit_is_refused_before_the_wire(self) -> None:
        self.limits(calls_per_minute=3)

        with clock(_NOW), vendor_wire() as wire:
            for x in range(3):
                self.assertEqual(self.fetch(x=x)[0], 200)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=3)

        self.assertEqual(wire.call_count, 3)

    def test_a_new_minute_is_a_new_window(self) -> None:
        self.limits(calls_per_minute=2)

        with vendor_wire() as wire:
            with clock(_NOW):
                self.fetch(x=0)
                self.fetch(x=1)
                with self.assertRaises(RateLimitExceededError):
                    self.fetch(x=2)
            with clock(_NOW + 60):
                self.assertEqual(self.fetch(x=2)[0], 200)

        self.assertEqual(wire.call_count, 3)

    def test_the_daily_limit_outlasts_the_minute(self) -> None:
        self.limits(calls_per_minute=100, calls_per_day=2)

        with vendor_wire() as wire:
            with clock(_NOW):
                self.fetch(x=0)
                self.fetch(x=1)
            with clock(_NOW + 120), self.assertRaises(RateLimitExceededError):
                self.fetch(x=2)
            with clock(_NOW + 86_400):
                self.assertEqual(self.fetch(x=2)[0], 200)

        self.assertEqual(wire.call_count, 3)

    def test_a_call_refused_by_the_day_does_not_use_up_the_minute(self) -> None:
        """Otherwise a full day would also fill every minute it is refused in, and the refusals would be counted
        against a window no call was made in."""
        self.limits(calls_per_minute=2, calls_per_day=1)

        with clock(_NOW), vendor_wire():
            self.fetch(x=0)
            for x in (1, 2, 3):
                with self.assertRaises(RateLimitExceededError):
                    self.fetch(x=x)
        self.limits(calls_per_day=None)
        with clock(_NOW), vendor_wire() as wire:
            self.assertEqual(self.fetch(x=4)[0], 200)

        self.assertEqual(wire.call_count, 1)

    def test_the_minimum_interval_holds(self) -> None:
        self.limits(min_interval_seconds=30.0)

        with vendor_wire() as wire:
            self.fetch(x=0)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=1)

        self.assertEqual(wire.call_count, 1)

    def test_a_disabled_service_is_refused_before_the_wire(self) -> None:
        self.limits(enabled=False)

        with vendor_wire() as wire, self.assertRaises(ServiceDisabledError):
            self.fetch()

        self.assertEqual(wire.call_count, 0)

    def test_a_thirty_day_budget_is_held_on_the_counters_too(self) -> None:
        """An admin can give the row any budget; none of them may send a call back to a locked row."""
        self.limits(calls_per_minute=None, calls_per_30_days=2)

        with clock(_NOW), vendor_wire() as wire:
            self.fetch(x=0)
            self.fetch(x=1)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=2)

        self.assertEqual(wire.call_count, 2)
        self.assertFalse(ApiCallLog.objects.filter(service=_SERVICE).exists())


class TheEnvironmentShareStillAppliesTests(_TalliedTestCase):
    """D26: a ``quota`` service's limits are this deployment's share of them, and no share is no call."""

    def test_staging_gets_its_share_of_the_minute(self) -> None:
        self.limits(calls_per_minute=4)

        with deployment("staging", share=0.5), clock(_NOW), vendor_wire() as wire:
            self.fetch(x=0)
            self.fetch(x=1)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=2)

        self.assertEqual(wire.call_count, 2)

    def test_an_override_names_the_service_its_own_share(self) -> None:
        self.limits(calls_per_minute=8)

        with deployment("staging", share=1.0, overrides={_SERVICE: 0.25}), clock(_NOW), vendor_wire() as wire:
            self.fetch(x=0)
            self.fetch(x=1)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=2)

        self.assertEqual(wire.call_count, 2)

    def test_development_makes_no_call_and_records_nothing(self) -> None:
        with deployment("development"), vendor_wire() as wire, self.assertRaises(EnvironmentRefusedError):
            self.fetch()

        self.assertEqual(wire.call_count, 0)
        self.assertEqual(call_tally.roll_up(), 0)
        self.assertFalse(ApiCallLog.objects.filter(service=_SERVICE).exists())


class NothingIsWrittenPerCallTests(_TalliedTestCase):
    """The point of tallying: no row, no lock and no count on the database for each call."""

    def _fresh_process(self) -> None:
        """What a newly started worker holds: the limits the roll-up published, and nothing of its own."""
        call_tally.publish_limits(_SERVICE)
        call_tally._limits.clear()

    def test_a_call_asks_the_database_nothing(self) -> None:
        self._fresh_process()
        with vendor_wire(), CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.fetch()[0], 200)

        self.assertEqual([query["sql"] for query in queries.captured_queries], [])

    def test_an_edit_to_the_row_reaches_the_next_call_without_a_read(self) -> None:
        """The API-limits page saves the row; its save publishes the new limits for every process."""
        row = ApiRateLimit.objects.get(service=_SERVICE)
        row.calls_per_minute = 1
        with self.captureOnCommitCallbacks(execute=True):
            row.save()
        call_tally._limits.clear()

        with clock(_NOW), vendor_wire() as wire, CaptureQueriesContext(connection) as queries:
            self.fetch(x=0)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=1)

        self.assertEqual(wire.call_count, 1)
        self.assertEqual([query["sql"] for query in queries.captured_queries], [])

    def test_the_rollup_keeps_the_published_limits_fresh(self) -> None:
        from django.core.cache import cache

        cache.delete(call_tally._limits_key(_SERVICE))

        call_tally.roll_up()

        self.assertIsNotNone(cache.get(call_tally._limits_key(_SERVICE)))

    def test_reading_the_limits_takes_no_lock(self) -> None:
        with vendor_wire(), CaptureQueriesContext(connection) as queries:
            self.fetch()

        statements = " ".join(query["sql"] for query in queries.captured_queries).upper()
        self.assertNotIn("FOR UPDATE", statements)
        self.assertNotIn("DASHBOARD_API_CALL_LOG", statements)

    def test_a_refusal_asks_the_database_nothing_either(self) -> None:
        """Under load the limiter refuses more than it admits, so the refusal is the path that has to be cheap."""
        self.limits(calls_per_minute=1)
        self._fresh_process()
        with clock(_NOW), vendor_wire():
            self.fetch(x=0)
            with CaptureQueriesContext(connection) as queries, self.assertRaises(RateLimitExceededError):
                self.fetch(x=1)

        self.assertEqual([query["sql"] for query in queries.captured_queries], [])


class _UnreachableStore:
    """A counter store that cannot be reached."""

    def __getattr__(self, name: str) -> object:
        def unreachable(*_args: object, **_kwargs: object) -> object:
            raise CacheUnavailableError(f"{name}: store unreachable")

        return unreachable


class ACounterOutageRefusesTests(_TalliedTestCase):
    """Never unlimited, and never back onto the database: with no store to count in, nothing can count the call."""

    @staticmethod
    @contextlib.contextmanager
    def _store_down() -> Iterator[None]:
        with mock.patch("urbanlens.dashboard.services.core.counters._ops", return_value=_UnreachableStore()):
            yield

    def test_the_call_is_refused_before_the_wire(self) -> None:
        with self._store_down(), vendor_wire() as wire, self.assertRaises(RateLimiterUnavailableError):
            self.fetch()

        self.assertEqual(wire.call_count, 0)
        self.assertFalse(ApiCallLog.objects.filter(service=_SERVICE).exists())

    def test_the_refusal_asks_the_database_nothing(self) -> None:
        call_tally.publish_limits(_SERVICE)
        with (
            self._store_down(),
            vendor_wire(),
            CaptureQueriesContext(connection) as queries,
            self.assertRaises(RateLimiterUnavailableError),
        ):
            self.fetch()

        self.assertEqual([query["sql"] for query in queries.captured_queries], [])

    def test_an_outcome_the_store_could_not_take_rides_along_with_the_next(self) -> None:
        """The store can go between admitting a call and recording it; the call was counted, and is recorded late
        rather than written to the database on the request."""
        with vendor_wire():
            with mock.patch.object(counters, "add_to_tally", side_effect=CounterUnavailableError("store down")):
                self.fetch(x=0)
            self.assertFalse(ApiCallLog.objects.filter(service=_SERVICE).exists())
            self.fetch(x=1)

        call_tally.roll_up()

        self.assertEqual(ApiCallLog.objects.filter(service=_SERVICE).aggregate(total=Sum("calls"))["total"], 2)

    def test_asking_ahead_counts_rolled_up_calls(self) -> None:
        """``service_is_permitted`` reads the ledger, where a rolled-up row stands for several calls."""
        self.limits(calls_per_minute=None, calls_per_day=2)
        with vendor_wire():
            self.fetch(x=0)
            self.fetch(x=1)
        call_tally.roll_up()

        self.assertFalse(check_rate_limit(_SERVICE))


class TheRollUpKeepsSpendVisibleTests(_TalliedTestCase):
    """What the API-limits page, the costs page and provider health read is still there, a minute late."""

    def _make_a_mixed_minute(self) -> None:
        answers = iter(
            [
                vendor_answer(200),
                vendor_answer(200),
                vendor_answer(404, b"", "text/plain"),
                vendor_answer(500, b"", "text/plain"),
            ]
        )

        def answer(*_args: object, **_kwargs: object) -> requests.Response:
            try:
                return next(answers)
            except StopIteration:
                raise requests.ConnectionError("vendor unreachable") from None

        self.limits(calls_per_minute=5)
        with clock(_NOW), vendor_wire(answer):
            for x in range(4):
                self.fetch(layer="satellite" if x < 3 else "street", x=x)
            with self.assertRaises(requests.ConnectionError):
                self.fetch(x=4)
            with self.assertRaises(RateLimitExceededError):
                self.fetch(x=5)

    def test_every_call_and_refusal_lands_in_the_ledger(self) -> None:
        self._make_a_mixed_minute()
        self.assertFalse(ApiCallLog.objects.filter(service=_SERVICE).exists(), "nothing is written per call")

        written = call_tally.roll_up()

        rows = ApiCallLog.objects.filter(service=_SERVICE)
        self.assertEqual(written, rows.count())
        self.assertEqual(rows.aggregate(total=Sum("calls"))["total"], 6)
        self.assertEqual(rows.filter(status_code=200, success=True).aggregate(n=Sum("calls"))["n"], 2)
        self.assertEqual(rows.filter(status_code=404, success=False).aggregate(n=Sum("calls"))["n"], 1)
        self.assertEqual(rows.filter(status_code=500).aggregate(n=Sum("calls"))["n"], 1)
        self.assertEqual(
            rows.filter(status_code__isnull=True, success=False, was_rate_limited=False).aggregate(n=Sum("calls"))["n"],
            1,
        )
        self.assertEqual(rows.filter(was_rate_limited=True).aggregate(n=Sum("calls"))["n"], 1)

    def test_each_row_is_dated_to_its_minute_and_belongs_to_nobody(self) -> None:
        self._make_a_mixed_minute()
        call_tally.roll_up()

        rows = ApiCallLog.objects.filter(service=_SERVICE)
        self.assertEqual(set(rows.values_list("created", flat=True)), {datetime.fromtimestamp(_MINUTE_START, tz=UTC)})
        self.assertFalse(rows.exclude(profile__isnull=True).exists())

    def test_no_row_names_a_coordinate(self) -> None:
        """Which tiles a viewer asked for is which places they looked at."""
        self._make_a_mixed_minute()
        call_tally.roll_up()

        for endpoint in ApiCallLog.objects.filter(service=_SERVICE).values_list("endpoint", flat=True):
            self.assertNotRegex(endpoint, r"/tile/\d", endpoint)

    def test_a_minute_is_rolled_up_once(self) -> None:
        self._make_a_mixed_minute()
        call_tally.roll_up()

        self.assertEqual(call_tally.roll_up(), 0)
        self.assertEqual(ApiCallLog.objects.filter(service=_SERVICE).aggregate(total=Sum("calls"))["total"], 6)

    def test_calls_made_while_it_runs_are_left_for_the_next_run(self) -> None:
        with clock(_NOW), vendor_wire():
            self.fetch(x=0)
        call_tally.roll_up()
        with clock(_NOW + 5), vendor_wire():
            self.fetch(x=1)

        call_tally.roll_up()

        rows = ApiCallLog.objects.filter(service=_SERVICE)
        self.assertEqual(rows.aggregate(total=Sum("calls"))["total"], 2)

    def test_a_ledger_that_cannot_be_written_keeps_the_tally(self) -> None:
        self._make_a_mixed_minute()

        with mock.patch.object(ApiCallLog.objects, "bulk_create", side_effect=DatabaseError("connection lost")):
            self.assertEqual(call_tally.roll_up(), 0)
        call_tally.roll_up()

        self.assertEqual(ApiCallLog.objects.filter(service=_SERVICE).aggregate(total=Sum("calls"))["total"], 6)

    def test_the_api_limits_page_counts_calls_not_rows(self) -> None:
        self._make_a_mixed_minute()
        call_tally.roll_up()

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}[_SERVICE]

        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["errors"], 3)

    def test_provider_health_judges_every_call_that_went_out(self) -> None:
        """Backoff reads the same ledger; a minute of tiles must weigh as many calls, not as one."""
        self._make_a_mixed_minute()
        call_tally.roll_up()

        counts = [
            item
            for item in provider_health._read_counts(datetime.fromtimestamp(_MINUTE_START - 60, tz=UTC))
            if item.provider == _SERVICE
        ]
        tally = provider_health._tally(counts)

        self.assertEqual((tally.ok, tally.empty, tally.failed, tally.refused), (2, 1, 2, 0))

    def test_the_rollup_runs_every_minute(self) -> None:
        from django.conf import settings

        from urbanlens.UrbanLens.egress import BEAT_EGRESS, BeatEgress

        entry = settings.FULL_BEAT_SCHEDULE["api-call-tally-rollup"]
        self.assertEqual(entry["task"], "urbanlens.dashboard.tasks.roll_up_api_call_tallies")
        self.assertEqual(entry["schedule"], 60)
        self.assertIs(BEAT_EGRESS["api-call-tally-rollup"], BeatEgress.INTERNAL)


class AdmissionTests(SimpleTestCase):
    """What an admission gives back when its call is never made."""

    def test_releasing_returns_every_window_it_counted(self) -> None:
        from urbanlens.dashboard.services.core import counters

        counters.hit("ul:test:window", 60, on_outage=counters.Outage.REFUSE)
        admission = call_tally.Admission(service=_SERVICE, endpoint="", minute=0, counted=["ul:test:window"])

        admission.release()

        self.assertEqual(counters.peek("ul:test:window", on_outage=counters.Outage.REFUSE), 0)
