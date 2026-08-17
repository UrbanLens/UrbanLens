"""The shared test helpers must fail when they are supposed to.

``run_concurrently`` and ``assert_agrees`` exist to make two error-prone patterns
routine - real-thread race tests, and holding a fast reimplementation to the
function it replaced. A helper that silently passes is worse than no helper,
because every test built on it inherits the false confidence.
"""

from __future__ import annotations

import threading
import time

import pytest

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.core.tests.testcase import SimpleTestCase


class AssertAgreesTests(SimpleTestCase):
    def test_identical_implementations_pass(self) -> None:
        assert_agrees(lambda n: n > 2, lambda n: n > 2, range(6))

    def test_a_disagreement_is_raised_with_its_direction(self) -> None:
        with pytest.raises(AssertionError) as caught:
            # Candidate is true for 2, where the reference is not.
            assert_agrees(lambda n: n > 2, lambda n: n >= 2, range(6))

        message = str(caught.value)
        self.assertIn("said yes where the reference said no", message)
        self.assertIn("1 subject", message)

    def test_the_other_direction_reads_differently(self) -> None:
        """"Wrongly hidden" and "wrongly shown" are different bugs; the message must say which."""
        with pytest.raises(AssertionError, match="said no where the reference said yes"):
            assert_agrees(lambda n: n >= 2, lambda n: n > 2, range(6))

    def test_every_disagreement_is_listed_not_just_the_first(self) -> None:
        with pytest.raises(AssertionError) as caught:
            assert_agrees(lambda _n: False, lambda _n: True, range(4))

        self.assertIn("4 subject(s)", str(caught.value))

    def test_the_describe_and_label_hooks_reach_the_message(self) -> None:
        with pytest.raises(AssertionError) as caught:
            assert_agrees(lambda _n: True, lambda _n: False, [7], describe=lambda n: f"row-{n}", label="fast_path")

        message = str(caught.value)
        self.assertIn("row-7", message)
        self.assertIn("fast_path", message)


class RunConcurrentlyTests(SimpleTestCase):
    def test_results_come_back_in_the_order_given(self) -> None:
        results = run_concurrently([lambda: "first", lambda: "second", lambda: "third"])

        self.assertEqual(results, ["first", "second", "third"])

    def test_all_callables_are_released_together(self) -> None:
        """The barrier is the point: without it a 'race' test runs sequentially."""
        seen: list[int] = []
        started = threading.Event()

        def slow() -> None:
            started.set()
            time.sleep(0.05)
            seen.append(1)

        def quick() -> None:
            started.wait(timeout=5)
            seen.append(2)

        run_concurrently([slow, quick])

        # The quick one finishes first only because both were already running.
        self.assertEqual(seen, [2, 1])

    def test_an_exception_in_a_thread_becomes_a_failure(self) -> None:
        def explode() -> None:
            raise ValueError("boom")

        with pytest.raises(AssertionError, match="boom"):
            run_concurrently([explode, lambda: None])

    def test_a_thread_that_never_finishes_is_reported_as_such(self) -> None:
        with pytest.raises(AssertionError, match="did not finish"):
            run_concurrently([lambda: time.sleep(5), lambda: None], timeout=1)
