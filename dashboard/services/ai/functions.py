"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    functions.py                                                                                       *
*        - Path:    /dashboard/services/ai/functions.py                                                                *
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
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from UrbanLens.dashboard.services.ai.message import MessageQueue

def estimate_tokens(prompt: str) -> int:
    """
    Estimate the number of tokens in a given text prompt.

        This method provides an approximation based on whitespace and common punctuation.


        Args:
            prompt (str):
                The text prompt to estimate the token count for.

        Returns:
            int:
                The estimated token count for the given prompt.
    """
    # Basic whitespace tokenization as a rough approximation
    tokens = prompt.split()

    # Further split on common punctuation to better approximate model tokenization
    punctuations = [".", ",", "!", "?", ";", ":", "-", "—", "(", ")", "[", "]", "{", "}", '"', "'"]
    refined_tokens = []
    for token in tokens:
        temp_token = [token]
        for punct in punctuations:
            temp_token = [subtoken for token in temp_token for subtoken in token.split(punct) if subtoken]
        refined_tokens.extend(temp_token)

    return len(refined_tokens)

def estimate_combined_tokens(messages: 'MessageQueue' | List[Dict[str, str]]) -> int:
    """
    Estimate the combined token count of a list of messages.
    
        This method provides an approximation based on whitespace and common punctuation.

        Args:
            messages (MessageQueue | List[Dict[str, str]]):
                The list of messages to estimate the combined token count for.

        Returns:
            int:
                The estimated combined token count for the given list of messages.
    """
    tokens = 0
    for message in messages:
        tokens += estimate_tokens(message['content'])
    return tokens