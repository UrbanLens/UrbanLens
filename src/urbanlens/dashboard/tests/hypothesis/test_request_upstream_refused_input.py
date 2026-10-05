"""A request-path upstream call whose input can never answer is the caller's to fix: a 400, never a cached outage."""

from __future__ import annotations

from typing import ClassVar

from django.core.cache import cache

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError, InputRejection
from urbanlens.dashboard.services.core.request_upstream import Outcome, RequestUpstream, refusal_json


class _Upstream(RequestUpstream):
    name: ClassVar[str] = "test_refused_input"


class RefusedInputTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def _refuse(self) -> object:
        raise ImpossibleInputError("test_refused_input", InputRejection.EMPTY_QUERY, "query is empty")

    def test_a_refused_input_answers_400_and_is_not_an_answer(self) -> None:
        result = _Upstream.call(self._refuse, key="k", ttl=60)

        self.assertIs(result.outcome, Outcome.REFUSED)
        self.assertFalse(result.ok)
        self.assertEqual(result.http_status, 400)
        self.assertIsInstance(result.error, ImpossibleInputError)
        self.assertEqual(refusal_json(result, {"results": []}).status_code, 400)

    def test_nothing_is_cached_for_a_refused_input(self) -> None:
        _Upstream.call(self._refuse, key="k", ttl=60)
        self.assertIsNone(_Upstream.cached("k"))

    def test_a_refusal_is_not_logged_as_an_upstream_failure(self) -> None:
        with self.assertNoLogs("urbanlens.dashboard.services.core.request_upstream", level="WARNING"):
            _Upstream.call(self._refuse)
