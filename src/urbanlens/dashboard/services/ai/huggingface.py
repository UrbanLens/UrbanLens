"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    huggingface.py                                                                                      *
*        - Path:    /dashboard/services/ai/huggingface.py                                                               *
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
import logging
from urbanlens.dashboard.services.ai.gateway import LLMGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'tgi'

class HuggingFaceGateway(LLMGateway):
    
    def _lookup_model(self, model_name: str | None) -> str:
        if not model_name:
            return DEFAULT_MODEL
        
        return super()._lookup_model(model_name)

    def setup(self, **kwargs):
        raise NotImplementedError("HuggingFaceGateway is not yet implemented. Implement abstractmethods, and generics, similar to cloudflare.py")

        if not self.api_url:
            self.api_url = settings.huggingface_ai_endpoint
        if not self.api_key:
            self.api_key = settings.huggingface_ai_api_key

        super().setup(**kwargs)

        if not self.api_url or not self.api_key:
            raise ValueError("Cloudflare AI Gateway requires an API URL and API Key.")