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
*        - Email:   jess@manlyphotos.com                                                                               *
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
from typing import Optional
from dashboard.services.ai.meta import SHORTEST_MESSAGE, MAX_TOKENS
from dashboard.services.ai.functions import estimate_combined_tokens, estimate_tokens

class MessageQueue:
    messages = []
    max_tokens : int = MAX_TOKENS

    def add_message(self, message : str, role : str = "user"):
        tokens = self.estimate_tokens(message)
        if tokens + SHORTEST_MESSAGE > self.max_tokens:
            raise ValueError(f"Message length {tokens} would exceed maximum token limit of {self.max_tokens}.")

        self.messages.append({
            "role": role,
            "content": message
        })

    def estimate_tokens(self, additional_prompt : Optional[str] = None) -> int:
        tokens = estimate_combined_tokens(self.messages)
        if additional_prompt:
            tokens += estimate_tokens(additional_prompt)

        return tokens
    
    def __iter__(self) -> iter:
        return iter(self.messages)
    
    def __len__(self) -> int:
        return len(self.messages)
    
    def __getitem__(self, index) -> dict:
        return self.messages[index]
    
    def __setitem__(self, index, value) -> None:
        self.messages[index] = value

    def __delitem__(self, index) -> None:
        del self.messages[index]

    def __str__(self) -> str:
        return str(self.messages)
    
    def __repr__(self) -> str:
        return repr(self.messages)
