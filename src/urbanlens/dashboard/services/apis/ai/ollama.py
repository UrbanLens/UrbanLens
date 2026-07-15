"""Ollama gateway - free, open-source, self-hosted vision-model photo keywording.

https://ollama.com/ - runs open-source vision models (e.g. LLaVA) locally, so
there's no external API call, no per-call cost, and no API key: just a base
URL pointing at the admin's own Ollama server (default
``http://localhost:11434``, matching Ollama's own default port).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import logging
from typing import ClassVar

import requests

from urbanlens.dashboard.services.ai.vision import _KEYWORD_PROMPT, _parse_keyword_text  # reuse the shared prompt/parsing, not worth duplicating
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True, kw_only=True)
class OllamaGateway(Gateway):
    """Gateway for a self-hosted Ollama server's vision-model generate endpoint."""

    service_key: ClassVar[str] = "ollama"
    paid_service: ClassVar[bool] = False

    base_url: str | None = field(default_factory=lambda: settings.ollama_base_url)
    model: str = field(default_factory=lambda: settings.ollama_vision_model)

    def describe_photo_keywords(self, image_bytes: bytes) -> list[str]:
        """Ask the local Ollama vision model for photo keywords.

        Args:
            image_bytes: JPEG bytes, already downscaled (never the full upload).

        Returns:
            Raw keyword strings; empty when no server is configured or the
            call fails.
        """
        if not self.base_url:
            return []

        payload = {
            "model": self.model,
            "prompt": _KEYWORD_PROMPT,
            "images": [base64.b64encode(image_bytes).decode("ascii")],
            "stream": False,
        }
        try:
            response = self.session.post(f"{self.base_url.rstrip('/')}/api/generate", json=payload, timeout=60)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("Ollama vision keyword generation failed (model=%s)", self.model, exc_info=True)
            return []
        return _parse_keyword_text(body.get("response") or "")
