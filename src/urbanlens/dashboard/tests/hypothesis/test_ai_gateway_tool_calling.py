"""Tests for LLMGateway.send_with_tools - the native tool-calling entry point (batch 2c).

Unlike send_prompt/send_prompt_list, this returns the raw InferenceResponse
(content blocks, stop_reason) rather than parsing an <ANSWER> tag - there is
no text protocol for a native-tool-calling caller to parse.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.ai.anthropic import AnthropicGateway
from urbanlens.dashboard.services.ai.inference_client import InferenceError, ToolSpec
from urbanlens_ai.schema import InferenceResponse, TextBlock, ToolUseBlock, Usage

_TOOLS = [ToolSpec(name="search_pins", description="search the user's pins", input_schema={"type": "object", "properties": {}})]


class SendWithToolsTests(SimpleTestCase):
    def setUp(self) -> None:
        # "claude-*" isn't a tiktoken-native model, so calculate_tokens falls
        # back to downloading the o200k_base encoding on first use - fine in
        # the sandboxed containers (baked at build time, see the Dockerfile),
        # but these are unit tests of send_with_tools's own logic, not of
        # tiktoken, and must not depend on network access or a warm cache.
        patcher = mock.patch.object(AnthropicGateway, "calculate_tokens", return_value=10)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _gateway(self) -> AnthropicGateway:
        # formatting="" - no <ANSWER> wrapping instruction; that protocol has
        # nothing to do with native tool calling.
        gateway = AnthropicGateway(formatting="", instructions="test instructions")
        gateway._inference_client = mock.Mock()
        return gateway

    def test_tools_reach_the_inference_request(self) -> None:
        gateway = self._gateway()
        gateway._inference_client.send.return_value = InferenceResponse(content=[TextBlock(text="hi")], stop_reason="end_turn", usage=Usage(output_tokens=5))

        gateway.send_with_tools("hello", _TOOLS)

        request = gateway._inference_client.send.call_args.args[0]
        self.assertEqual(request.tools, _TOOLS)

    def test_ignores_a_gateway_constructed_with_the_answer_formatting(self) -> None:
        # Even a caller that forgot formatting="" must not leak the <ANSWER>
        # wrapping instruction into a native tool-calling call - it has
        # nothing to do with real tool use and would only confuse the model.
        gateway = AnthropicGateway(instructions="test instructions")  # default formatting, not ""
        gateway._inference_client = mock.Mock()
        gateway._inference_client.send.return_value = InferenceResponse(content=[TextBlock(text="hi")], stop_reason="end_turn", usage=Usage(output_tokens=5))

        gateway.send_with_tools("hello", _TOOLS)

        request = gateway._inference_client.send.call_args.args[0]
        self.assertNotIn("<FORMATTING>", request.system)
        self.assertNotIn("ANSWER", request.system)

    def test_formatting_is_restored_after_the_call(self) -> None:
        gateway = AnthropicGateway(formatting="custom formatting", instructions="test instructions")
        gateway._inference_client = mock.Mock()
        gateway._inference_client.send.return_value = InferenceResponse(content=[TextBlock(text="hi")], stop_reason="end_turn", usage=Usage(output_tokens=5))

        gateway.send_with_tools("hello", _TOOLS)

        self.assertEqual(gateway.formatting, "custom formatting")

    def test_returns_the_raw_response_unparsed(self) -> None:
        gateway = self._gateway()
        response = InferenceResponse(content=[ToolUseBlock(id="tu_1", name="search_pins", input={"query": "steel"})], stop_reason="tool_use", usage=Usage(output_tokens=10))
        gateway._inference_client.send.return_value = response

        result = gateway.send_with_tools("hello", _TOOLS)

        self.assertIs(result, response)
        self.assertEqual(result.stop_reason, "tool_use")
        tool_use = result.content[0]
        self.assertIsInstance(tool_use, ToolUseBlock)
        self.assertEqual(tool_use.name, "search_pins")

    def test_text_reply_round_trips_through_response_text(self) -> None:
        gateway = self._gateway()
        gateway._inference_client.send.return_value = InferenceResponse(content=[TextBlock(text="Here are your pins.")], stop_reason="end_turn", usage=Usage(output_tokens=5))

        result = gateway.send_with_tools("hello", _TOOLS)

        self.assertEqual(result.text, "Here are your pins.")

    def test_records_received_tokens_from_usage(self) -> None:
        gateway = self._gateway()
        gateway._inference_client.send.return_value = InferenceResponse(content=[TextBlock(text="hi")], stop_reason="end_turn", usage=Usage(output_tokens=42))

        gateway.send_with_tools("hello", _TOOLS)

        self.assertEqual(gateway.received_tokens, 42)

    def test_inference_error_returns_none(self) -> None:
        gateway = self._gateway()
        gateway._inference_client.send.side_effect = InferenceError("boom")

        self.assertIsNone(gateway.send_with_tools("hello", _TOOLS))

    def test_empty_content_returns_none(self) -> None:
        gateway = self._gateway()
        gateway._inference_client.send.return_value = InferenceResponse(content=[], stop_reason="end_turn", usage=Usage())

        self.assertIsNone(gateway.send_with_tools("hello", _TOOLS))

    def test_no_tools_is_an_empty_list_on_the_request(self) -> None:
        gateway = self._gateway()
        gateway._inference_client.send.return_value = InferenceResponse(content=[TextBlock(text="hi")], stop_reason="end_turn", usage=Usage(output_tokens=1))

        gateway.send_with_tools("hello", [])

        request = gateway._inference_client.send.call_args.args[0]
        self.assertEqual(request.tools, [])
