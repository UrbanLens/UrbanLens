"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    message.py                                                                                         *
*        - Path:    /dashboard/services/ai/message.py                                                                  *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-03-21                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-21     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from urbanlens.dashboard.services.ai.functions import estimate_combined_tokens, estimate_tokens
from urbanlens.dashboard.services.ai.meta import MAX_TOKENS, SHORTEST_MESSAGE


class MessageType(TypedDict):
    role: str
    content: str


class MessageQueue:
    messages: list[MessageType] = []
    max_tokens: int = MAX_TOKENS

    def add_message(self, message: str, role: str = "user") -> None:
        tokens = self.estimate_tokens(message)
        if tokens + SHORTEST_MESSAGE > self.max_tokens:
            raise ValueError(f"Message length {tokens} would exceed maximum token limit of {self.max_tokens}.")

        self.messages.append(
            {
                "role": role,
                "content": message,
            },
        )

    def estimate_tokens(self, additional_prompt: str | None = None) -> int:
        tokens = estimate_combined_tokens(self.messages)
        if additional_prompt:
            tokens += estimate_tokens(additional_prompt)

        return tokens

    def __iter__(self) -> iter[MessageType]:
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
