"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    __init__.py                                                                                        *
*        - Path:    /dashboard/services/ai/__init__.py                                                                 *
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
from urbanlens.dashboard.services.ai.meta import SHORTEST_MESSAGE, MAX_TOKENS
from urbanlens.dashboard.services.ai.functions import estimate_combined_tokens, estimate_tokens
from urbanlens.dashboard.services.ai.message import MessageQueue
from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.dashboard.services.ai.huggingface import HuggingFaceGateway
from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway
from urbanlens.dashboard.services.ai.openai import OpenAIGateway