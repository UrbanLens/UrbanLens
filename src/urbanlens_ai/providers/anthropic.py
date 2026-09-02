"""Anthropic provider adapter."""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolParam

from urbanlens_ai import policy
from urbanlens_ai.providers.base import ProviderAdapter, ProviderError
from urbanlens_ai.schema import ImagePart, InferenceRequest, InferenceResponse, Message, StopReason, TextBlock, ToolUseBlock, Usage

#: Anthropic's own stop_reason values that map onto our normalized set
#: unchanged; anything else (``stop_sequence``, ``pause_turn``, ``refusal``,
#: or a future value this adapter doesn't know yet) becomes ``"other"``
#: rather than raising, so a new Anthropic API addition degrades to "the
#: turn ended for some reason" instead of a hard failure.
_KNOWN_STOP_REASONS: frozenset[str] = frozenset({"end_turn", "max_tokens", "tool_use"})


class AnthropicAdapter(ProviderAdapter):
    """Translates :class:`InferenceRequest` to and from Anthropic's Messages API."""

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=policy.ANTHROPIC_BASE_URL,
            timeout=policy.PROVIDER_TIMEOUT_SECONDS,
            max_retries=policy.PROVIDER_MAX_RETRIES,
        )

    @staticmethod
    def _content(message: Message) -> Any:
        """Anthropic content for one message: a bare string, or typed blocks when it carries an image."""
        if isinstance(message.content, str):
            return message.content
        blocks: list[dict[str, Any]] = []
        for part in message.parts:
            if isinstance(part, ImagePart):
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": part.media_type, "data": part.data}})
            else:
                blocks.append({"type": "text", "text": part.text})
        return blocks

    def send(self, request: InferenceRequest) -> InferenceResponse:
        messages: list[MessageParam] = [MessageParam(role=message.role, content=self._content(message)) for message in request.messages]
        tools: list[ToolParam] = [ToolParam(name=tool.name, description=tool.description, input_schema=tool.input_schema) for tool in request.tools]

        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            response = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=messages,
                **kwargs,
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic call failed: {exc}") from exc

        content: list[TextBlock | ToolUseBlock] = []
        for block in response.content:
            if block.type == "text":
                content.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                content.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input) if isinstance(block.input, dict) else {}))

        stop_reason: StopReason = response.stop_reason if response.stop_reason in _KNOWN_STOP_REASONS else "other"

        return InferenceResponse(
            content=content,
            stop_reason=stop_reason,
            usage=Usage(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens),
        )
