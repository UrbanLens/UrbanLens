"""A provider call honours the caller's budget, on the HTTP hop and at the provider."""

from __future__ import annotations

from unittest import mock

from urbanlens_ai import policy
from urbanlens_ai.schema import InferenceRequest, Message

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.ai.inference_client import HOP_ALLOWANCE_SECONDS, RemoteInferenceClient


def _request(provider: str = "anthropic", **fields) -> InferenceRequest:
    return InferenceRequest(
        provider=provider, model="claude-x", messages=[Message(role="user", content="hi")], max_tokens=10, **fields
    )


class AttemptLimitsTests(SimpleTestCase):
    def test_no_budget_keeps_the_service_defaults(self) -> None:
        self.assertEqual(
            policy.attempt_limits(None), (float(policy.PROVIDER_TIMEOUT_SECONDS), policy.PROVIDER_MAX_RETRIES)
        )

    def test_a_budget_is_one_attempt_capped_at_the_budget(self) -> None:
        self.assertEqual(policy.attempt_limits(4.0), (4.0, 0))

    def test_a_generous_budget_is_still_capped_at_the_provider_timeout(self) -> None:
        self.assertEqual(policy.attempt_limits(10_000.0)[0], float(policy.PROVIDER_TIMEOUT_SECONDS))


class RemoteHopTimeoutTests(SimpleTestCase):
    def _send(self, request: InferenceRequest) -> float:
        client = RemoteInferenceClient("http://ai-inference", "token", timeout_seconds=90.0)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"content": [], "stop_reason": "end_turn"}
        with mock.patch(
            "urbanlens.dashboard.services.ai.inference_client.requests.post", return_value=response
        ) as post:
            client.send(request)
        return post.call_args.kwargs["timeout"]

    def test_the_hop_waits_only_a_little_longer_than_the_budget(self) -> None:
        self.assertEqual(self._send(_request(timeout_seconds=7.0)), 7.0 + HOP_ALLOWANCE_SECONDS)

    def test_without_a_budget_the_hop_uses_its_configured_timeout(self) -> None:
        self.assertEqual(self._send(_request()), 90.0)


class AdapterBudgetTests(SimpleTestCase):
    def test_the_anthropic_adapter_makes_one_attempt_within_the_budget(self) -> None:
        from urbanlens_ai.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(api_key="key")
        with mock.patch.object(adapter, "_client") as client:
            client.with_options.return_value.messages.create.side_effect = RuntimeError("stop here")
            with self.assertRaises(RuntimeError):
                adapter.send(_request(timeout_seconds=3.0))

        client.with_options.assert_called_once_with(timeout=3.0, max_retries=0)

    def test_the_anthropic_adapter_keeps_its_defaults_without_a_budget(self) -> None:
        from urbanlens_ai.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(api_key="key")
        with mock.patch.object(adapter, "_client") as client:
            client.messages.create.side_effect = RuntimeError("stop here")
            with self.assertRaises(RuntimeError):
                adapter.send(_request())

        client.with_options.assert_not_called()

    def test_the_openai_adapter_makes_one_attempt_within_the_budget(self) -> None:
        from urbanlens_ai.providers.openai import OpenAIAdapter

        adapter = OpenAIAdapter(api_key="key")
        with mock.patch.object(adapter, "_client") as client:
            client.with_options.return_value.chat.completions.create.side_effect = RuntimeError("stop here")
            with self.assertRaises(RuntimeError):
                adapter.send(_request(provider="openai", timeout_seconds=3.0))

        client.with_options.assert_called_once_with(timeout=3.0, max_retries=0)

    def test_the_cloudflare_adapter_times_its_request_to_the_budget(self) -> None:
        from urbanlens_ai.providers.cloudflare import CloudflareAdapter

        adapter = CloudflareAdapter.__new__(CloudflareAdapter)
        with (
            mock.patch.object(CloudflareAdapter, "_post", side_effect=RuntimeError("stop here")) as post,
            self.assertRaises(RuntimeError),
        ):
            adapter.send(_request(provider="cloudflare", timeout_seconds=3.0))

        self.assertEqual(post.call_args.kwargs["timeout"], 3.0)
