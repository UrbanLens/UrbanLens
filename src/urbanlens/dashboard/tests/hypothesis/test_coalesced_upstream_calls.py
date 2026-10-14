"""
Identical upstream asks made together cost one call.

Five panel sources on one Private Pin page each asked the same parcel lookup for the same coordinate, in
separate tasks started within a second of each other: 205 parcel lookups in an hour for 70 pins.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.core.cache import cache
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.coalesce import coalesced


class CoalescedTests(SimpleTestCase):
    def test_a_second_ask_is_answered_from_the_first(self) -> None:
        compute = mock.Mock(return_value={"uuid": "p-1"})

        first = coalesced("t:parcel", compute, ttl=60)
        second = coalesced("t:parcel", compute, ttl=60)

        self.assertEqual(first, second)
        compute.assert_called_once()

    def test_none_is_an_answer_too(self) -> None:
        compute = mock.Mock(return_value=None)

        coalesced("t:none", compute, ttl=60)
        coalesced("t:none", compute, ttl=60)

        compute.assert_called_once()

    def test_a_concurrent_ask_waits_for_the_one_in_flight(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow() -> str:
            calls.append(1)
            started.set()
            release.wait(5)
            return "answer"

        results: list[str] = []
        leader = threading.Thread(target=lambda: results.append(coalesced("t:slow", slow, ttl=60)))
        leader.start()
        started.wait(5)
        follower = threading.Thread(target=lambda: results.append(coalesced("t:slow", slow, ttl=60, poll_seconds=0.01)))
        follower.start()
        release.set()
        leader.join(5)
        follower.join(5)

        self.assertEqual(results, ["answer", "answer"])
        self.assertEqual(len(calls), 1)

    def test_a_failure_is_not_shared(self) -> None:
        compute = mock.Mock(side_effect=[RuntimeError("down"), "answer"])

        with pytest.raises(RuntimeError):
            coalesced("t:fail", compute, ttl=60)
        self.assertEqual(coalesced("t:fail", compute, ttl=60), "answer")

    def test_an_unreachable_cache_just_makes_the_call(self) -> None:
        compute = mock.Mock(return_value="answer")

        with (
            mock.patch.object(cache, "get", side_effect=ConnectionError),
            mock.patch.object(cache, "add", side_effect=ConnectionError),
            mock.patch.object(cache, "set", side_effect=ConnectionError),
        ):
            self.assertEqual(coalesced("t:down", compute, ttl=60), "answer")
