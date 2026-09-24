"""The paid-API limiter reserves before the call and refuses when it cannot count (G2-24, G2-25)."""

from __future__ import annotations

from unittest import mock

from django.db import DatabaseError

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.ai import vision
from urbanlens.dashboard.services.ai.inference_client import ClassificationLabel, ClassifyResponse
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.core.rate_limiter import (
    ServiceDisabledError,
    api_call_slot,
    check_rate_limit,
    get_limit_config,
)


class ACountThatCannotBeReadTests(TestCase):
    """G2-24: the config read failed closed for billable services; the count query beside it failed open."""

    def _unreadable_counts(self):  # noqa: ANN202
        return mock.patch.object(ApiCallLog.objects, "for_service", side_effect=DatabaseError("connection lost"))

    def test_a_billable_service_is_refused(self) -> None:
        config = get_limit_config("google_geocoding")
        with self._unreadable_counts():
            self.assertFalse(
                check_rate_limit("google_geocoding", config), "a paid API ran uncapped when its count failed"
            )

    def test_a_free_service_is_allowed(self) -> None:
        config = get_limit_config("overpass")
        with self._unreadable_counts():
            self.assertTrue(check_rate_limit("overpass", config))


class TheCheckIsAReservationTests(TestCase):
    """G2-25: a second caller arriving while the first call was in flight passed the same check."""

    def setUp(self) -> None:
        super().setUp()
        get_limit_config(vision.SERVICE_PHOTO_CLASSIFIER)
        ApiRateLimit.objects.filter(service=vision.SERVICE_PHOTO_CLASSIFIER).update(
            calls_per_minute=1, calls_per_day=None, calls_per_30_days=None
        )

    def test_a_call_in_flight_holds_its_place(self) -> None:
        upstream_calls: list[str] = []
        nested: list[list[tuple[str, float]]] = []

        def classify(_request):  # noqa: ANN001, ANN202
            upstream_calls.append("call")
            if len(upstream_calls) == 1:
                # A concurrent request arrives while this one is still waiting on the provider.
                nested.append(vision.classify_photo(b"second"))
            return ClassifyResponse(labels=[ClassificationLabel(label="mill", score=0.9)])

        client = mock.Mock()
        client.classify.side_effect = classify
        with mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client):
            first = vision.classify_photo(b"first")

        self.assertEqual(first, [("mill", 0.9)])
        self.assertEqual(nested, [[]], "the second caller was let through")
        self.assertEqual(len(upstream_calls), 1, f"a limit of 1 per minute made {len(upstream_calls)} provider calls")

    def test_the_reserved_row_records_the_outcome(self) -> None:
        client = mock.Mock()
        client.classify.return_value = ClassifyResponse(labels=[])
        with mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client):
            vision.classify_photo(b"x")

        rows = ApiCallLog.objects.filter(service=vision.SERVICE_PHOTO_CLASSIFIER, was_rate_limited=False)
        self.assertEqual(rows.count(), 1, "a reservation and a separate log row were both written")
        self.assertTrue(rows.get().success)
        self.assertIsNotNone(rows.get().response_ms)


class TheSlotTests(TestCase):
    def test_a_disabled_service_is_refused_before_the_block_runs(self) -> None:
        get_limit_config("trivia_moderation")
        ApiRateLimit.objects.filter(service="trivia_moderation").update(enabled=False)
        ran = []
        with self.assertRaises(ServiceDisabledError), api_call_slot("trivia_moderation"):
            ran.append(1)
        self.assertEqual(ran, [])

    def test_a_block_that_raises_is_recorded_as_failed(self) -> None:
        with self.assertRaises(RuntimeError), api_call_slot("trivia_moderation", endpoint="model-x"):
            raise RuntimeError("provider exploded")
        row = ApiCallLog.objects.filter(service="trivia_moderation").get()
        self.assertFalse(row.success)
        self.assertEqual(row.endpoint, "model-x")

    def test_an_llm_feature_is_refused_by_its_own_limit(self) -> None:
        """The budgeted LLM features logged spend but never asked the limiter first."""
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.trivia.classifier import classify_trivia_question

        get_limit_config("trivia_moderation")
        ApiRateLimit.objects.filter(service="trivia_moderation").update(enabled=False)
        gateway = mock.Mock(model="m", cost=None)
        with mock.patch("urbanlens.dashboard.services.trivia.classifier.get_gateway", return_value=gateway):
            verdict = classify_trivia_question("Q?", "A", Location(official_name="Mill"))
        gateway.send_prompt.assert_not_called()
        self.assertFalse(verdict.approved)

    def test_the_module_keeps_one_billable_rule(self) -> None:
        self.assertTrue(rate_limiter._is_billable("a-service-nobody-declared"))
        self.assertFalse(rate_limiter._is_billable("overpass"))
