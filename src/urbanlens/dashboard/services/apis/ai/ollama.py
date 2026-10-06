"""Ollama gateway - free, open-source, self-hosted vision-model photo keywording."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import logging
from typing import ClassVar

import requests

from urbanlens.dashboard.services.ai.vision import _KEYWORD_PROMPT, _parse_keyword_text  # reuse the shared...
from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError, annotate_calls
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

#: ``done_reason`` values for a request that loaded or unloaded the model and generated nothing.
_NO_GENERATION_REASONS = frozenset({"load", "unload"})
#: ``done_reason`` for a generation stopped by its output budget rather than finished by the model.
_CUT_OFF_REASON = "length"
#: Most of an Ollama error message kept in a log line.
_ERROR_DETAIL_CHARS = 200


def _reported_tokens(response: requests.Response) -> tuple[int | None, int | None]:
    """The prompt and completion token counts Ollama reports with a non-streamed answer.

    Args:
        response: A ``/api/generate`` response.

    Returns:
        ``(prompt_eval_count, eval_count)``; either is None when the server sent none.
    """
    body = response.json()
    prompt, completion = body.get("prompt_eval_count"), body.get("eval_count")
    return (prompt if isinstance(prompt, int) else None), (completion if isinstance(completion, int) else None)


def _answer_text(body: object) -> str | None:
    """The text of a finished, non-streamed ``/api/generate`` answer, or None when ``body`` is not one.

    Ollama reports an error as ``{"error": "..."}``: with a 4xx or 5xx status, or, once a response has started, with
    the 200 it already sent. A finished generation is an object whose ``response`` is a string and whose ``done`` is
    true. ``done_reason`` ``load`` or ``unload`` means the model was loaded or unloaded and nothing was generated, and
    ``length`` with no text means the output budget ran out (a thinking model can spend it all) before the model said
    anything.

    Args:
        body: The decoded JSON body.

    Returns:
        The generated text, which may be empty when the model finished and named nothing - that is still an answer;
        None for anything else.
    """
    if not isinstance(body, dict) or body.get("error"):
        return None
    text = body.get("response")
    if not isinstance(text, str) or body.get("done") is False:
        return None
    reason = body.get("done_reason")
    if reason is not None and not isinstance(reason, str):
        return None
    if reason in _NO_GENERATION_REASONS or (reason == _CUT_OFF_REASON and not text.strip()):
        return None
    return text


@dataclass(slots=True, kw_only=True)
class OllamaGateway(Gateway):
    """Gateway for a self-hosted Ollama server's vision-model generate endpoint."""

    service_key: ClassVar[str] = "ollama"
    paid_service: ClassVar[bool] = False

    base_url: str | None = field(default_factory=lambda: settings.ollama_base_url)
    model: str = field(default_factory=lambda: settings.ollama_vision_model)

    def describe_photo_keywords(self, image_bytes: bytes) -> list[str] | None:
        """Ask the local Ollama vision model for photo keywords.

        Args:
            image_bytes: JPEG bytes, already downscaled (never the full upload).

        Returns:
            Raw keyword strings, empty when the model answered with none; None when no answer came - no server is
            configured, the call was refused before it was sent, it failed, or the body was not a finished
            generation - so nothing is known about the photo (P322).
        """
        if not self.base_url:
            return None

        payload = {
            "model": self.model,
            "prompt": _KEYWORD_PROMPT,
            "images": [base64.b64encode(image_bytes).decode("ascii")],
            "stream": False,
        }
        try:
            with annotate_calls(model=self.model, usage=_reported_tokens):
                response = self.session.post(f"{self.base_url.rstrip('/')}/api/generate", json=payload, timeout=60)
            response.raise_for_status()
            body = response.json()
        except RequestCancelledError as exc:
            logger.info("Ollama vision keyword generation was not sent (model=%s): %s", self.model, exc)
            return None
        except requests.exceptions.RequestException:
            logger.warning("Ollama vision keyword generation failed (model=%s)", self.model, exc_info=True)
            return None
        text = _answer_text(body)
        if text is None:
            error = body.get("error") if isinstance(body, dict) else None
            detail = str(error)[:_ERROR_DETAIL_CHARS] if error else "no finished generation in the body"
            logger.warning("Ollama vision keyword generation did not answer (model=%s): %s", self.model, detail)
            return None
        return _parse_keyword_text(text)
