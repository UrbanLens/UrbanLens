"""AI vision calls must record their estimated cost on the ApiCallLog row."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.ai import vision
from urbanlens.dashboard.services.ai.inference_client import InferenceResponse, TextBlock, Usage


def _client(text: str, usage: Usage) -> mock.Mock:
    """A stand-in inference client returning one text answer with the given usage."""
    client = mock.Mock()
    client.send.return_value = InferenceResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=usage)
    return client


class OpenAIVisionCostLoggingTests(TestCase):
    """The token-derived cost estimate must reach the ApiCallLog row."""

    def setUp(self) -> None:
        # _vision_target() reads this to choose the provider; the cost
        # arithmetic under test only applies to the OpenAI path (Cloudflare
        # Workers AI bills per request, not per token).
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(ai_provider="openai")

    def _run(self, usage: Usage) -> ApiCallLog:
        with mock.patch(
            "urbanlens.dashboard.services.ai.inference_client.get_inference_client",
            return_value=_client("brick, mill, rust", usage),
        ):
            keywords = vision.describe_photo_keywords(b"fake-image-bytes")

        self.assertEqual(keywords, ["brick", "mill", "rust"])
        entry = ApiCallLog.objects.filter(service=vision.SERVICE_AI_PHOTO_KEYWORDS).latest("created")
        self.assertTrue(entry.success)
        return entry

    def test_the_cost_estimate_is_recorded(self) -> None:
        entry = self._run(Usage(input_tokens=1000, output_tokens=200))

        self.assertIsNotNone(entry.cost_estimate, "cost was computed from token usage but never stored")
        self.assertGreater(entry.cost_estimate, Decimal(0))

    def test_a_response_without_usage_still_records_a_fallback_cost(self) -> None:
        """No usage counts means the fallback token estimate, not a null cost."""
        entry = self._run(Usage())

        self.assertIsNotNone(entry.cost_estimate)
        self.assertGreater(entry.cost_estimate, Decimal(0))

    def test_more_tokens_cost_more(self) -> None:
        cheap = self._run(Usage(input_tokens=100, output_tokens=10)).cost_estimate
        pricey = self._run(Usage(input_tokens=10_000, output_tokens=1_000)).cost_estimate

        self.assertGreater(pricey, cheap)

    def test_a_failed_call_records_a_failure_row_and_returns_nothing(self) -> None:
        from urbanlens.dashboard.services.ai.inference_client import InferenceError

        client = mock.Mock()
        client.send.side_effect = InferenceError("ai-inference returned HTTP 502")
        with mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client):
            self.assertEqual(vision.describe_photo_keywords(b"fake-image-bytes"), [])

        entry = ApiCallLog.objects.filter(service=vision.SERVICE_AI_PHOTO_KEYWORDS).latest("created")
        self.assertFalse(entry.success)
