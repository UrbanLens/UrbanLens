"""AI vision calls must record their estimated cost on the ApiCallLog row.

``CLAUDE.md``: "When calling any API, track usage and cost per call (keep a
running estimate). This is required groundwork for future cost reporting."

Gateway-based services get this for free - ``rate_limiter``'s HTTP wrapper reads
``ServiceDefaults.cost_per_call`` and passes it to ``log_api_call`` itself. The
AI services talk to SDK clients instead of that wrapper, so each one passes its
own cost, and the OpenAI vision path is the one that computes the *most accurate*
figure of any of them: real prompt/completion token counts off the response,
priced per model. It was computing that, logging it to the application log, and
then not passing it on - so the row that cost reporting will actually read had a
null cost for the single most expensive call type in the app.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.ai import vision


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


def _response(text: str, usage: _Usage | None):
    message = mock.Mock()
    message.content = text
    choice = mock.Mock()
    choice.message = message
    response = mock.Mock()
    response.choices = [choice]
    response.usage = usage
    return response


class OpenAIVisionCostLoggingTests(TestCase):
    """The token-derived cost estimate must reach the ApiCallLog row."""

    def _run(self, usage: _Usage | None) -> ApiCallLog:
        client = mock.Mock()
        client.chat.completions.create.return_value = _response("brick, mill, rust", usage)
        with mock.patch("openai.OpenAI", return_value=client):
            keywords = vision._openai_vision_keywords(b"fake-image-bytes")

        self.assertEqual(keywords, ["brick", "mill", "rust"])
        entry = ApiCallLog.objects.filter(service=vision.SERVICE_AI_PHOTO_KEYWORDS).latest("created")
        self.assertTrue(entry.success)
        return entry

    def test_the_cost_estimate_is_recorded(self) -> None:
        entry = self._run(_Usage(prompt_tokens=1000, completion_tokens=200))

        self.assertIsNotNone(entry.cost_estimate, "cost was computed from token usage but never stored")
        self.assertGreater(entry.cost_estimate, Decimal(0))

    def test_a_response_without_usage_still_records_a_fallback_cost(self) -> None:
        """No ``usage`` block means the fallback token estimate, not a null cost."""
        entry = self._run(None)

        self.assertIsNotNone(entry.cost_estimate)
        self.assertGreater(entry.cost_estimate, Decimal(0))

    def test_more_tokens_cost_more(self) -> None:
        cheap = self._run(_Usage(prompt_tokens=100, completion_tokens=10)).cost_estimate
        pricey = self._run(_Usage(prompt_tokens=10_000, completion_tokens=1_000)).cost_estimate

        self.assertGreater(pricey, cheap)
