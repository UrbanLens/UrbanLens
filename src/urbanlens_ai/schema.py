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


class TextPart(BaseModel):
    """A run of text inside a multi-part message."""

    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """An image inside a multi-part message, as base64-encoded bytes.

    Base64 rather than a URL on purpose: a URL would be something the
    inference service has to *fetch*, which is exactly the capability this
    tier is built to not have (see ``docs/AI_PIPELINE.md`` - the egress
    allowlist carries provider hosts and nothing else). The caller reads the
    image itself and sends the bytes inline.

    Callers must send an already-downscaled image
    (``services.photos.photo_keywords.downscaled_jpeg_bytes`` caps the longest
    edge at 512px), never a full-resolution upload -
    :data:`~urbanlens_ai.policy.MAX_IMAGE_BYTES` is the backstop for when
    they don't.
    """

    type: Literal["image"] = "image"
    media_type: Literal["image/jpeg", "image/png", "image/webp", "image/gif"] = "image/jpeg"
    #: Standard base64 (not a ``data:`` URL) of the raw image bytes.
    data: str


MessagePart = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class Message(BaseModel):
    """One user/assistant turn in the conversation.

    The system prompt is never a message here - it is
    :attr:`InferenceRequest.system`, a separate top-level field, matching
    Anthropic's own API shape. Every provider adapter reconstructs whatever
    its own SDK needs from that (OpenAI folds it back into a leading
    ``system``-role message; Cloudflare does the same).

    ``content`` is a bare string for the ordinary text case and a list of
    parts when the turn carries an image. Both shapes reach every adapter,
    which is what lets vision be an ordinary message rather than a second
    endpoint with its own auth, policy and adapter machinery.
    """

    role: Role
    content: str | list[MessagePart]

    @property
    def parts(self) -> list[MessagePart]:
        """``content`` as a part list, wrapping the bare-string form."""
        return [TextPart(text=self.content)] if isinstance(self.content, str) else self.content

    @property
    def text(self) -> str:
        """Just the text of this message, ignoring any images."""
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))

    @property
    def images(self) -> list[ImagePart]:
        """Just the images in this message, in order."""
        return [part for part in self.parts if isinstance(part, ImagePart)]


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


class ClassificationLabel(BaseModel):
    """One label an image classifier returned, with its confidence."""

    label: str
    score: float


class ClassifyRequest(BaseModel):
    """An image-classification call - image in, scored labels out.

    Deliberately *not* an :class:`InferenceRequest`. A classifier takes no
    prompt, holds no conversation, spends no tokens and returns labels rather
    than text; folding it into the chat schema would mean a request type
    where half the fields are meaningless. Vision *with* a prompt is a chat
    completion and goes through :class:`InferenceRequest` as an
    :class:`ImagePart` - see that class.
    """

    provider: Provider
    model: str
    image: ImagePart


class ClassifyResponse(BaseModel):
    """Normalized classifier output, highest confidence first."""

    labels: list[ClassificationLabel] = Field(default_factory=list)
