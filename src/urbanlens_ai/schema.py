"""Wire schema shared between an inference caller and the inference service.

Deliberately Django-free, like every other module in this package - see
``urbanlens_ai/__init__.py`` for why. ``dashboard.services.ai.inference_client``
is the only place under ``urbanlens`` that imports these types.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Role = Literal["user", "assistant"]
Provider = Literal["anthropic", "openai", "cloudflare"]
StopReason = Literal["end_turn", "max_tokens", "tool_use", "other"]


class Message(BaseModel):
    """One user/assistant turn in the conversation.

    The system prompt is never a message here - it is
    :attr:`InferenceRequest.system`, a separate top-level field, matching
    Anthropic's own API shape. Every provider adapter reconstructs whatever
    its own SDK needs from that (OpenAI folds it back into a leading
    ``system``-role message; Cloudflare does the same).
    """

    role: Role
    content: str


class ToolSpec(BaseModel):
    """A tool the model may call, in JSON-Schema form.

    Maps 1:1 onto each provider SDK's own tool-declaration shape (Anthropic's
    ``tools=[{name, description, input_schema}]``, OpenAI's function-calling
    schema) and, not coincidentally, onto MCP's ``Tool`` type - see the
    plan's MCP decision for why that made a dedicated MCP server unnecessary
    for this pass.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


ContentBlock = Annotated[TextBlock | ToolUseBlock, Field(discriminator="type")]


class Usage(BaseModel):
    """Token accounting, provider-reported when available.

    Fields are ``None`` when the provider's response didn't include usage
    (not every Cloudflare Workers AI model does) - the caller falls back to
    its own token estimate rather than treating a missing count as zero cost.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None


class InferenceRequest(BaseModel):
    """A single, normalized call to a provider's chat/messages endpoint."""

    provider: Provider
    model: str
    system: str = ""
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int
    temperature: float | None = None


class InferenceResponse(BaseModel):
    """The provider's answer, normalized to one shape regardless of provider."""

    content: list[ContentBlock]
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)

    @property
    def text(self) -> str:
        """Concatenated text from every text block; empty if there are none."""
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))
