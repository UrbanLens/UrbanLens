"""Vision AI helpers: photo keyword description and image classification.

Separate from the text-only ``LLMGateway`` hierarchy because vision requests
have provider-specific payload shapes (data-URL content parts for OpenAI,
byte arrays for Cloudflare Workers AI). Callers are expected to pass an
already-downscaled image (see ``photo_keywords.downscaled_jpeg_bytes``) -
never full-resolution uploads.

Every call is recorded via ``rate_limiter.log_api_call`` and respects the
admin-configurable per-service limits, with a running cost estimate logged
per call (groundwork for API cost reporting).
"""

from __future__ import annotations

import base64
from decimal import Decimal
import logging
import time
from typing import Any

import requests

from urbanlens.dashboard.services.rate_limiter import check_rate_limit, log_api_call, service_is_enabled

logger = logging.getLogger(__name__)

#: Service keys (rate limits configurable at /site-admin/api-limits/).
SERVICE_AI_PHOTO_KEYWORDS = "ai_photo_keywords"
SERVICE_PHOTO_CLASSIFIER = "cloudflare_image_classifier"

#: Cloudflare Workers AI models used here.
_CF_VISION_MODEL = "@cf/llava-hf/llava-1.5-7b-hf"
_CF_CLASSIFIER_MODEL = "@cf/microsoft/resnet-50"

_KEYWORD_PROMPT = (
    "Describe this photo as searchable keywords for a photo library. "
    "List 8-15 short keywords or two-word phrases covering the subject, setting, "
    "architecture, objects, weather, and mood. Respond with ONLY the keywords, "
    "comma-separated, no numbering and no other text."
)

#: Rough OpenAI vision token estimate for a <=512px image plus prompt/response.
#: Good enough for a running cost estimate; exact usage is logged when the API
#: returns it.
_OPENAI_VISION_FALLBACK_TOKENS = (900, 120)


def _rate_limit_gate(service: str) -> bool:
    """Check the admin-configured enable/rate-limit state for a service key."""
    if not service_is_enabled(service):
        log_api_call(service, success=False, was_service_disabled=True)
        return False
    if not check_rate_limit(service):
        log_api_call(service, success=False, was_rate_limited=True)
        return False
    return True


def _parse_keyword_text(text: str) -> list[str]:
    """Split a comma/newline-separated keyword response into clean keywords."""
    parts: list[str] = []
    for chunk in text.replace("\n", ",").split(","):
        keyword = chunk.strip(" .;:-*#\"'")
        if keyword:
            parts.append(keyword)
    return parts


def _openai_vision_keywords(image_bytes: bytes) -> list[str]:
    """Ask OpenAI's vision-capable chat API for photo keywords."""
    from openai import OpenAI

    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.services.ai.openai import DEFAULT_MODEL, OpenAIGateway
    from urbanlens.UrbanLens.settings.app import settings

    site = SiteSettings.get_current()
    model = site.openai_model or DEFAULT_MODEL
    client = OpenAI(api_key=settings.openai_api_key)
    data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")

    started = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _KEYWORD_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                ],
            },
        ],
        max_tokens=300,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    sent_tokens, received_tokens = _OPENAI_VISION_FALLBACK_TOKENS
    usage = getattr(response, "usage", None)
    if usage is not None:
        sent_tokens = usage.prompt_tokens or sent_tokens
        received_tokens = usage.completion_tokens or received_tokens
    cost_sent, cost_received = OpenAIGateway.MODEL_COSTS.get(model, OpenAIGateway.DEFAULT_COST_PER_THOUSAND)
    estimated_cost = (Decimal(sent_tokens) * cost_sent + Decimal(received_tokens) * cost_received) / 1000
    logger.info(
        "AI photo keywords via OpenAI %s: %d+%d tokens, est. $%s, %dms",
        model,
        sent_tokens,
        received_tokens,
        round(estimated_cost, 5),
        elapsed_ms,
    )
    log_api_call(SERVICE_AI_PHOTO_KEYWORDS, success=True, response_ms=elapsed_ms, endpoint=f"openai:{model}")

    body = response.choices[0].message.content or ""
    return _parse_keyword_text(body)


def _cloudflare_post(model: str, payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any] | None:
    """POST one Workers AI request, returning the parsed JSON or None."""
    from urbanlens.UrbanLens.settings.app import settings

    if not settings.cloudflare_worker_ai_endpoint or not settings.cloudflare_ai_api_key:
        return None
    url = f"{str(settings.cloudflare_worker_ai_endpoint).rstrip('/')}/{model.lstrip('/')}"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {settings.cloudflare_ai_api_key}"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _cloudflare_vision_keywords(image_bytes: bytes) -> list[str]:
    """Ask Cloudflare Workers AI (LLaVA) for photo keywords."""
    started = time.monotonic()
    data = _cloudflare_post(_CF_VISION_MODEL, {"image": list(image_bytes), "prompt": _KEYWORD_PROMPT, "max_tokens": 256})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if data is None:
        return []
    log_api_call(SERVICE_AI_PHOTO_KEYWORDS, success=True, response_ms=elapsed_ms, endpoint=f"cloudflare:{_CF_VISION_MODEL}")
    logger.info("AI photo keywords via Cloudflare %s: %dms (per-request Workers AI pricing)", _CF_VISION_MODEL, elapsed_ms)
    result = data.get("result") or {}
    return _parse_keyword_text(str(result.get("description") or result.get("response") or ""))


def describe_photo_keywords(image_bytes: bytes) -> list[str]:
    """Generate descriptive keywords for a (downscaled) photo via the site's AI provider.

    Caller is responsible for permission checks (site/profile AI toggles and
    the AI photo processing subscription feature); this function only handles
    the provider call, rate limiting, and cost logging.

    Args:
        image_bytes: JPEG bytes, already downscaled (never the full upload).

    Returns:
        Raw keyword strings (possibly empty on failure - errors are logged).
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if not _rate_limit_gate(SERVICE_AI_PHOTO_KEYWORDS):
        return []

    site = SiteSettings.get_current()
    try:
        if site.ai_provider == "openai":
            return _openai_vision_keywords(image_bytes)
        return _cloudflare_vision_keywords(image_bytes)
    except Exception:
        logger.exception("AI photo keyword generation failed (provider=%s)", site.ai_provider)
        log_api_call(SERVICE_AI_PHOTO_KEYWORDS, success=False)
        return []


def classify_photo(image_bytes: bytes) -> list[tuple[str, float]]:
    """Classify a (downscaled) photo's content via Cloudflare's ResNet-50 model.

    Args:
        image_bytes: JPEG bytes, already downscaled.

    Returns:
        (label, confidence) pairs, highest confidence first; empty on failure.
    """
    if not _rate_limit_gate(SERVICE_PHOTO_CLASSIFIER):
        return []

    started = time.monotonic()
    try:
        data = _cloudflare_post(_CF_CLASSIFIER_MODEL, {"image": list(image_bytes)}, timeout=30)
    except Exception:
        logger.exception("Photo classification failed")
        log_api_call(SERVICE_PHOTO_CLASSIFIER, success=False)
        return []
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if data is None:
        return []
    log_api_call(SERVICE_PHOTO_CLASSIFIER, success=True, response_ms=elapsed_ms, endpoint=f"cloudflare:{_CF_CLASSIFIER_MODEL}")

    labels: list[tuple[str, float]] = []
    for entry in data.get("result") or []:
        label = str(entry.get("label") or "").strip()
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if label:
            labels.append((label, score))
    labels.sort(key=lambda pair: pair[1], reverse=True)
    return labels
