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
from UrbanLens.dashboard.services.ai.meta import SHORTEST_MESSAGE, MAX_TOKENS
from UrbanLens.dashboard.services.ai.functions import estimate_combined_tokens, estimate_tokens
from UrbanLens.dashboard.services.ai.message import MessageQueue
from UrbanLens.dashboard.services.ai.gateway import LLMGateway
from UrbanLens.dashboard.services.ai.huggingface import HuggingFaceGateway
from UrbanLens.dashboard.services.ai.cloudflare import CloudflareGateway
from UrbanLens.dashboard.services.ai.openai import OpenAIGateway