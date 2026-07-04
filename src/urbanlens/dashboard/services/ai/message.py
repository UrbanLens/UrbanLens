from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from urbanlens.dashboard.services.ai.functions import estimate_combined_tokens, estimate_tokens
from urbanlens.dashboard.services.ai.meta import MAX_TOKENS, SHORTEST_MESSAGE

if TYPE_CHECKING:
    from collections.abc import Iterator


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: str


MessageType = SystemMessage | UserMessage | AssistantMessage


class MessageQueue:
    def __init__(self, max_tokens: int = MAX_TOKENS):
        self.messages: list[MessageType] = []
        self.max_tokens: int = max_tokens

    def add_message(self, message: str, role: Literal["user", "system", "assistant"] = "user") -> None:
        tokens = self.estimate_tokens(message)
        if tokens + SHORTEST_MESSAGE > self.max_tokens:
            raise ValueError(
                f"Message length {tokens} would exceed maximum token limit of {self.max_tokens}. Message = {message[:20]}...{message[-20:]}",
            )

        msg: MessageType
        if role == "system":
            msg = SystemMessage(role="system", content=message)
        elif role == "assistant":
            msg = AssistantMessage(role="assistant", content=message)
        else:
            msg = UserMessage(role="user", content=message)
        self.messages.append(msg)

    def estimate_tokens(self, additional_prompt: str | None = None) -> int:
        tokens = estimate_combined_tokens(self.messages)
        if additional_prompt:
            tokens += estimate_tokens(additional_prompt)

        return tokens

    def __iter__(self) -> Iterator[MessageType]:
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index: int) -> MessageType:
        return self.messages[index]

    def __setitem__(self, index: int, value: MessageType) -> None:
        self.messages[index] = value

    def __delitem__(self, index: int) -> None:
        del self.messages[index]

    def __str__(self) -> str:
        return str(self.messages)

    def __repr__(self) -> str:
        return repr(self.messages)
