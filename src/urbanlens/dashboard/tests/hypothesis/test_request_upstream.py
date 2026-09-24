"""The request-path upstream policy: cache, throttle, slot and deadline in one call.

The properties that matter are the ones a view cannot see from its own response: a failure is never
cached, a slot stays held by a fetch the request abandoned, and a cache hit costs the caller nothing
against their throttle.
"""

from __future__ import annotations

from concurrent.futures import Future
import threading
import time
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, UpstreamBusyError
from urbanlens.dashboard.services.core.request_upstream import Outcome, RequestUpstream, wait_all
from urbanlens.dashboard.services.security.throttle import Rate


class _Upstream(RequestUpstream):
    name = "test.upstream"
    deadline = 0.3
    rate = Rate(limit=3, window_seconds=60)

    @classmethod
    def limit(cls) -> int:
        return 1


class _OtherUpstream(RequestUpstream):
    name = "test.other"
    deadline = 0.3

    @classmethod
    def limit(cls) -> int:
        return 1


class _Case(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        _Upstream.reset()
        _OtherUpstream.reset()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _hang(self) -> list[str]:
        self.release.wait(timeout=10)
        return ["late"]

    def _wait_for_slot(self, upstream: type[RequestUpstream]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if upstream.semaphore().acquire(blocking=False):
                upstream.semaphore().release()
                return
            time.sleep(0.01)
        self.fail("the slot was never released")


class CachingTests(_Case):
    def test_an_answer_is_cached_and_served_without_asking_again(self) -> None:
        fetch = mock.Mock(return_value=["a"])

        first = _Upstream.call(fetch, key="q", ttl=60)
        second = _Upstream.call(fetch, key="q", ttl=60)

        self.assertEqual((first.outcome, first.value), (Outcome.FRESH, ["a"]))
        self.assertEqual((second.outcome, second.value), (Outcome.CACHED, ["a"]))
        self.assertEqual(fetch.call_count, 1)

    def test_an_empty_answer_is_still_an_answer(self) -> None:
        fetch = mock.Mock(return_value=[])

        _Upstream.call(fetch, key="q", ttl=60)
        again = _Upstream.call(fetch, key="q", ttl=60)

        self.assertTrue(again.cached)
        self.assertEqual(fetch.call_count, 1)

    def test_a_failure_is_never_cached(self) -> None:
        fetch = mock.Mock(side_effect=[GatewayRequestError("down"), ["b"]])

        failed = _Upstream.call(fetch, key="q", ttl=60)
        recovered = _Upstream.call(fetch, key="q", ttl=60)

        self.assertEqual(failed.outcome, Outcome.FAILED)
        self.assertIsInstance(failed.error, GatewayRequestError)
        self.assertEqual((recovered.outcome, recovered.value), (Outcome.FRESH, ["b"]))

    def test_cacheable_can_refuse_a_returned_value(self) -> None:
        fetch = mock.Mock(return_value={"partial": True})

        _Upstream.call(fetch, key="q", ttl=60, cacheable=lambda value: not value["partial"])
        _Upstream.call(fetch, key="q", ttl=60, cacheable=lambda value: not value["partial"])

        self.assertEqual(fetch.call_count, 2)

    def test_a_bug_in_the_fetch_is_raised_not_swallowed(self) -> None:
        with self.assertRaises(KeyError):
            _Upstream.call(mock.Mock(side_effect=KeyError("bug")))

    def test_a_busy_upstream_passes_its_wait_on(self) -> None:
        result = _Upstream.call(mock.Mock(side_effect=UpstreamBusyError("slow down", retry_after=42)))

        self.assertEqual((result.outcome, result.retry_after), (Outcome.FAILED, 42))


class DeadlineAndSlotTests(_Case):
    def test_a_hanging_fetch_costs_the_request_only_the_deadline(self) -> None:
        started = time.monotonic()
        result = _Upstream.call(self._hang, key="q", ttl=60)

        self.assertEqual(result.outcome, Outcome.TIMED_OUT)
        self.assertLess(time.monotonic() - started, 2)

    def test_the_abandoned_fetch_keeps_its_slot_so_the_next_caller_is_told_busy(self) -> None:
        _Upstream.call(self._hang, key="q", ttl=60)
        fetch = mock.Mock(return_value=["never"])

        second = _Upstream.call(fetch, key="other", ttl=60)

        self.assertEqual(second.outcome, Outcome.BUSY)
        fetch.assert_not_called()

    def test_the_abandoned_fetch_releases_its_slot_and_caches_when_it_finishes(self) -> None:
        _Upstream.call(self._hang, key="q", ttl=60)
        self.release.set()
        self._wait_for_slot(_Upstream)

        # The late answer is cached even though nobody was waiting for it.
        deadline = time.monotonic() + 5
        while _Upstream.cached("q") is None and time.monotonic() < deadline:
            time.sleep(0.01)
        result = _Upstream.call(mock.Mock(return_value=["unused"]), key="q", ttl=60)
        self.assertEqual((result.outcome, result.value), (Outcome.CACHED, ["late"]))

    def test_a_fetch_that_never_started_gives_its_slot_back(self) -> None:
        never_runs: Future[list[str]] = Future()
        with mock.patch("urbanlens.dashboard.services.core.request_upstream.submit_bounded", return_value=never_runs):
            result = _Upstream.call(mock.Mock())

        self.assertEqual(result.outcome, Outcome.TIMED_OUT)
        self.assertTrue(never_runs.cancelled())
        self._wait_for_slot(_Upstream)

    def test_each_upstream_has_its_own_slots(self) -> None:
        _Upstream.call(self._hang)

        other = _OtherUpstream.call(mock.Mock(return_value=["ok"]))

        self.assertEqual(other.outcome, Outcome.FRESH)

    def test_wait_all_bounds_several_calls_by_one_budget(self) -> None:
        slow = _Upstream.start(self._hang)
        fast = _OtherUpstream.start(mock.Mock(return_value=["ok"]))

        started = time.monotonic()
        wait_all([slow, fast], timeout=0.3)

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(fast.result(0).value, ["ok"])
        self.assertEqual(slow.result(0).outcome, Outcome.TIMED_OUT)


class ThrottleTests(_Case):
    def test_uncached_calls_past_the_rate_are_refused_with_a_wait(self) -> None:
        results = [_Upstream.call(mock.Mock(return_value=[i]), key=f"q{i}", ttl=60, caller="user:1") for i in range(4)]

        self.assertEqual([r.outcome for r in results], [Outcome.FRESH] * 3 + [Outcome.THROTTLED])
        self.assertGreaterEqual(results[-1].retry_after or 0, 1)

    def test_cache_hits_do_not_count_against_the_caller(self) -> None:
        _Upstream.call(mock.Mock(return_value=["a"]), key="q", ttl=60, caller="user:1")
        hits = [_Upstream.call(mock.Mock(), key="q", ttl=60, caller="user:1") for _ in range(5)]

        self.assertTrue(all(hit.cached for hit in hits))
        self.assertEqual(
            _Upstream.call(mock.Mock(return_value=["b"]), key="new", ttl=60, caller="user:1").outcome, Outcome.FRESH
        )

    def test_callers_have_separate_budgets(self) -> None:
        for i in range(3):
            _Upstream.call(mock.Mock(return_value=[i]), key=f"q{i}", caller="user:1")

        self.assertEqual(_Upstream.call(mock.Mock(return_value=["x"]), caller="user:2").outcome, Outcome.FRESH)
