"""`ProcessMemo` holds a value for its interval and nothing it was told not to keep."""

from __future__ import annotations

import threading
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.process_memo import ProcessMemo

_CLOCK = "urbanlens.dashboard.services.core.process_memo.time.monotonic"


class ProcessMemoTests(SimpleTestCase):
    def test_a_value_is_computed_once_within_the_interval(self) -> None:
        memo = ProcessMemo[int](ttl_seconds=30)
        compute = mock.Mock(side_effect=[1, 2])

        self.assertEqual([memo.get(compute), memo.get(compute)], [1, 1])
        self.assertEqual(compute.call_count, 1)

    def test_it_is_recomputed_after_the_interval(self) -> None:
        memo = ProcessMemo[int](ttl_seconds=30)
        compute = mock.Mock(side_effect=[1, 2])
        with mock.patch(_CLOCK, return_value=100.0):
            memo.get(compute)
        with mock.patch(_CLOCK, return_value=131.0):
            self.assertEqual(memo.get(compute), 2)

    def test_a_held_none_is_still_held(self) -> None:
        memo = ProcessMemo[int | None](ttl_seconds=30)
        compute = mock.Mock(return_value=None)

        memo.get(compute)
        memo.get(compute)
        self.assertEqual(compute.call_count, 1)

    def test_a_value_it_may_not_keep_is_recomputed(self) -> None:
        memo = ProcessMemo[str](ttl_seconds=30)
        compute = mock.Mock(side_effect=["unknown", "current", "later"])

        results = [memo.get(compute, keep=lambda state: state != "unknown") for _ in range(3)]
        self.assertEqual(results, ["unknown", "current", "current"])

    def test_an_exception_holds_nothing(self) -> None:
        memo = ProcessMemo[int](ttl_seconds=30)
        with self.assertRaises(RuntimeError):
            memo.get(mock.Mock(side_effect=RuntimeError("down")))
        self.assertEqual(memo.get(lambda: 7), 7)

    def test_clear_forgets_the_value(self) -> None:
        memo = ProcessMemo[int](ttl_seconds=30)
        memo.get(lambda: 1)
        memo.clear()
        self.assertEqual(memo.get(lambda: 2), 2)

    def test_concurrent_callers_share_one_computation(self) -> None:
        memo = ProcessMemo[int](ttl_seconds=30)
        started, release = threading.Event(), threading.Event()
        calls: list[int] = []

        def slow() -> int:
            calls.append(1)
            started.set()
            release.wait(5)
            return 42

        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(memo.get(slow))) for _ in range(4)]
        threads[0].start()
        started.wait(5)
        for thread in threads[1:]:
            thread.start()
        release.set()
        for thread in threads:
            thread.join(5)

        self.assertEqual(results, [42] * 4)
        self.assertEqual(len(calls), 1)
