"""Tests for CloudflareGateway.MODEL_COSTS (docs/PROBLEMS.md follow-up).

SiteSettings.cloudflare_model is free text with no dropdown constraint, so an
admin can point it at any Workers AI model - but MODEL_COSTS previously had
exactly one entry (the site default), so every other model silently fell back
to LLMGateway.DEFAULT_COST_PER_THOUSAND's generic estimate instead of that
model's real published price (only a WARNING-level log to notice it happened).
Added real entries (developers.cloudflare.com/workers-ai/platform/pricing,
verified 2026-07-19) for the other mainstream chat models most likely to
actually get picked.
"""

from __future__ import annotations

from decimal import Decimal

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.ai.cloudflare import DEFAULT_MODEL, CloudflareGateway


def _gateway(model: str) -> CloudflareGateway:
    return CloudflareGateway(api_key="test-key", api_url="https://example.com/ai", model=model)


#: cost is rounded to 2 decimal places (see LLMGateway.cost) - a 1000-token
#: sample rounds to $0.00 for every model here, so these tests price out a
#: million tokens each way instead, landing on a meaningfully non-zero amount.
_SAMPLE_TOKENS = 1_000_000


class CloudflareModelCostsTests(SimpleTestCase):
    def test_default_model_cost_is_unchanged(self) -> None:
        """Regression guard: the one entry that already existed must keep its value."""
        gw = _gateway(DEFAULT_MODEL)
        gw.send_tokens(_SAMPLE_TOKENS)
        gw.receive_tokens(_SAMPLE_TOKENS)
        self.assertEqual(gw.cost, Decimal("0.30"))

    def test_missing_model_falls_back_to_default_model_not_the_generic_estimate(self) -> None:
        """factory.py constructs with ``model=site.cloudflare_model or None`` when the site's
        configured model is blank - CloudflareGateway._lookup_model resolves that ``None`` to
        DEFAULT_MODEL, so this must price at DEFAULT_MODEL's real rate rather than missing
        MODEL_COSTS and silently using the generic fallback.
        """
        gw = CloudflareGateway(api_key="test-key", api_url="https://example.com/ai", model=None)
        self.assertEqual(gw.model, DEFAULT_MODEL)
        gw.send_tokens(_SAMPLE_TOKENS)
        gw.receive_tokens(_SAMPLE_TOKENS)
        self.assertEqual(gw.cost, Decimal("0.30"))

    def test_other_mainstream_models_have_real_costs_not_the_generic_fallback(self) -> None:
        generic_cost = round(_SAMPLE_TOKENS * CloudflareGateway.DEFAULT_COST_PER_THOUSAND[0] / 1000 + _SAMPLE_TOKENS * CloudflareGateway.DEFAULT_COST_PER_THOUSAND[1] / 1000, 2)
        #: exact published-pricing cost for _SAMPLE_TOKENS tokens each way, not just
        #: "isn't the generic fallback" - pins the actual MODEL_COSTS values so a
        #: typo'd digit in an entry fails the test instead of surviving because the
        #: wrong number still happens to differ from the (much larger) generic one.
        expected_costs = {
            "@cf/meta/llama-3.1-8b-instruct": Decimal("1.11"),
            "@cf/meta/llama-3.2-1b-instruct": Decimal("0.23"),
            "@cf/meta/llama-3.2-3b-instruct": Decimal("0.39"),
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast": Decimal("2.55"),
            "@cf/google/gemma-3-12b-it": Decimal("0.90"),
            "@cf/qwen/qwen3-30b-a3b-fp8": Decimal("0.39"),
        }
        for model, expected in expected_costs.items():
            with self.subTest(model=model):
                gw = _gateway(model)
                self.assertIn(model, CloudflareGateway.MODEL_COSTS, f"{model} missing a real MODEL_COSTS entry")
                gw.send_tokens(_SAMPLE_TOKENS)
                gw.receive_tokens(_SAMPLE_TOKENS)
                self.assertNotEqual(gw.cost, generic_cost)
                self.assertEqual(gw.cost, expected)

    def test_llama_3_1_8b_cost_matches_published_pricing(self) -> None:
        """$0.282 per M input tokens / $0.827 per M output tokens."""
        gw = _gateway("@cf/meta/llama-3.1-8b-instruct")
        gw.send_tokens(_SAMPLE_TOKENS)
        gw.receive_tokens(_SAMPLE_TOKENS)
        self.assertEqual(gw.cost, Decimal("1.11"))

    def test_unrecognized_model_still_falls_back_to_the_generic_estimate(self) -> None:
        gw = _gateway("@cf/some-vendor/a-brand-new-model-not-yet-catalogued")
        gw.send_tokens(_SAMPLE_TOKENS)
        gw.receive_tokens(_SAMPLE_TOKENS)
        expected = round(_SAMPLE_TOKENS * CloudflareGateway.DEFAULT_COST_PER_THOUSAND[0] / 1000 + _SAMPLE_TOKENS * CloudflareGateway.DEFAULT_COST_PER_THOUSAND[1] / 1000, 2)
        self.assertEqual(gw.cost, expected)
