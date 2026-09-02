"""OpenAI provider adapter."""

from __future__ import annotations

import json
from typing import Any

import openai
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)

from urbanlens_ai import policy
from urbanlens_ai.providers.base import ProviderAdapter, ProviderError
from urbanlens_ai.schema import ImagePart, InferenceRequest, InferenceResponse, Message, StopReason, TextBlock, ToolUseBlock, Usage

#: OpenAI's own ``finish_reason`` values mapped onto our normalized set.
#: ``content_filter``/``function_call``/anything unrecognized becomes
#: ``"other"`` rather than raising.
_FINISH_REASON_MAP: dict[str, StopReason] = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


class OpenAIAdapter(ProviderAdapter):
    """Translates :class:`InferenceRequest` to and from OpenAI's Chat Completions API."""

    def __init__(self, api_key: str) -> None:
        self._client = OpenAI(
            api_key=api_key,
            base_url=policy.OPENAI_BASE_URL,
            timeout=policy.PROVIDER_TIMEOUT_SECONDS,
            max_retries=policy.PROVIDER_MAX_RETRIES,
        )

    @staticmethod
    def _user_content(message: Message) -> Any:
        """OpenAI content for one user message: a bare string, or content parts when it carries an image.

        Images go as a ``data:`` URL, which is how the Chat Completions API
        takes inline bytes - not a fetchable URL, so this stays consistent
        with the "the inference tier fetches nothing" rule (see
        :class:`~urbanlens_ai.schema.ImagePart`). ``detail: "low"`` matches
        what the caller's 512px downscale can actually support and keeps the
        image's token cost flat.
        """
        if isinstance(message.content, str):
            return message.content
        parts: list[dict[str, Any]] = []
        for part in message.parts:
            if isinstance(part, ImagePart):
                parts.append({"type": "image_url", "image_url": {"url": f"data:{part.media_type};base64,{part.data}", "detail": "low"}})
            else:
                parts.append({"type": "text", "text": part.text})
        return parts

    def send(self, request: InferenceRequest) -> InferenceResponse:
        messages: list[ChatCompletionMessageParam] = []
        if request.system:
            messages.append(ChatCompletionSystemMessageParam(role="system", content=request.system))
        for message in request.messages:
            if message.role == "assistant":
                # An assistant turn is always plain text - nothing generates
                # an image back into the transcript.
                messages.append(ChatCompletionAssistantMessageParam(role="assistant", content=message.text))
            else:
                messages.append(ChatCompletionUserMessageParam(role="user", content=self._user_content(message)))

        tools: list[ChatCompletionToolParam] = [ChatCompletionToolParam(type="function", function={"name": tool.name, "description": tool.description, "parameters": tool.input_schema}) for tool in request.tools]

        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            response = self._client.chat.completions.create(
                model=request.model,
                messages=messages,
                max_tokens=request.max_tokens,
                **kwargs,
            )
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI call failed: {exc}") from exc

        choice = response.choices[0]
        content: list[TextBlock | ToolUseBlock] = []
        if choice.message.content:
            content.append(TextBlock(text=choice.message.content))
        for call in choice.message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            content.append(ToolUseBlock(id=call.id, name=call.function.name, input=arguments if isinstance(arguments, dict) else {}))

        stop_reason = _FINISH_REASON_MAP.get(choice.finish_reason, "other")

        usage = Usage()
        if response.usage:
            usage = Usage(input_tokens=response.usage.prompt_tokens, output_tokens=response.usage.completion_tokens)

        return InferenceResponse(content=content, stop_reason=stop_reason, usage=usage)
