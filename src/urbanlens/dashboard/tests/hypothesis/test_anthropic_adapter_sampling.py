"""The Anthropic adapter sends a request's temperature in the body, where SDK 1.x still accepts it.

anthropic 1.x removed ``temperature`` from ``messages.create``; passing it raises ``TypeError`` before any request is
made. ``autospec`` holds the mock to the installed SDK's real signature, so these fail the way production would.
"""

from __future__ import annotations

from unittest import mock

from urbanlens_ai.schema import InferenceRequest, Message

from urbanlens.core.tests.testcase import SimpleTestCase


def _request(**fields) -> InferenceRequest:
    return InferenceRequest(
        provider="anthropic", model="claude-x", messages=[Message(role="user", content="hi")], max_tokens=10, **fields
    )


class AnthropicTemperatureTests(SimpleTestCase):
    def _sent(self, request: InferenceRequest) -> dict:
        from urbanlens_ai.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(api_key="key")
        with mock.patch.object(
            adapter._client.messages, "create", autospec=True, side_effect=RuntimeError("stop here")
        ) as create:
            self.assertRaises(RuntimeError, adapter.send, request)
        create.assert_called_once()
        return create.call_args.kwargs

    def test_a_temperature_travels_in_the_request_body(self) -> None:
        sent = self._sent(_request(temperature=0.2))
        self.assertEqual(sent.get("extra_body"), {"temperature": 0.2})
        self.assertNotIn("temperature", sent)

    def test_no_temperature_sends_no_extra_body(self) -> None:
        self.assertNotIn("extra_body", self._sent(_request()))
