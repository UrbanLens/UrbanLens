"""Vision AI helpers: photo keyword description and image classification.

Separate from the text-only ``LLMGateway`` hierarchy because these calls
aren't conversations - there is no transcript, no tool loop, and no
``<ANSWER>`` protocol, just one image and one question. What they *do* share
is the sandbox: like every other provider call in this app, they go through
``services.ai.inference_client`` to ``ai-inference``, which is the only tier
holding provider API keys. Nothing in this module builds a provider client or
reads a provider credential - see ``docs/AI_PIPELINE.md``.

Callers pass the stored 512px copy the sandbox already wrote
(``photo_keywords.analysis_jpeg_bytes``), never a full-resolution upload and
never bytes this process decoded itself. ``urbanlens_ai.policy.MAX_IMAGE_BYTES``
is the backstop if a caller forgets.

Every call is recorded via ``rate_limiter.log_api_call`` and respects the
admin-configurable per-service limits, with a running cost estimate logged
per call. That accounting stays here, on the app side, for the same reason
``LLMGateway`` keeps its own: ``ai-inference`` has no database and no idea
what a service key or a cost bucket is.
"""

from __future__ import annotations

import base64
from decimal import Decimal
import logging
import time
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.core.rate_limiter import check_rate_limit, log_api_call, service_is_enabled

if TYPE_CHECKING:
    from urbanlens_ai.schema import Provider

logger = logging.getLogger(__name__)

#: Service keys (rate limits configurable at /site-admin/api-limits/).
SERVICE_AI_PHOTO_KEYWORDS = "ai_photo_keywords"
SERVICE_PHOTO_CLASSIFIER = "cloudflare_image_classifier"

#: Cloudflare Workers AI models used here.
_CF_VISION_MODEL = "@cf/llava-hf/llava-1.5-7b-hf"
_CF_CLASSIFIER_MODEL = "@cf/microsoft/resnet-50"

#: Response budget for a keyword list - a couple of dozen short phrases.
_KEYWORD_MAX_TOKENS = 300

_KEYWORD_PROMPT = (
    "Describe this photo as searchable keywords for a photo library. "
    "List 8-15 short keywords or two-word phrases covering the subject, setting, "
    "architecture, objects, weather, and mood. Respond with ONLY the keywords, "
    "comma-separated, no numbering and no other text."
)

#: Characters stripped from each keyword returned by a model - list
#: punctuation and quoting it may wrap a phrase in.
_KEYWORD_STRIP_CHARS = " .;:-*#\"'"

#: Rough OpenAI vision token estimate for a <=512px image plus prompt/response,
#: used only when the provider's response carries no usage of its own. Good
#: enough for a running cost estimate.
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
        keyword = chunk.strip(_KEYWORD_STRIP_CHARS)
        if keyword:
            parts.append(keyword)
    return parts


def _vision_target() -> tuple[Provider, str]:
    """The ``(provider, model)`` the site's AI settings select for a vision call.

    Mirrors the provider dispatch every other AI feature does through
    ``services.ai.factory``, but resolved here: this module talks to the
    inference client directly rather than through an ``LLMGateway``, because
    there is no conversation for a gateway to manage.

    Returns:
        The provider name and model identifier to send this call to.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.services.ai.openai import DEFAULT_MODEL

    site = SiteSettings.get_current()
    if site.ai_provider == "openai":
        return "openai", site.openai_model or DEFAULT_MODEL
    return "cloudflare", _CF_VISION_MODEL


def _openai_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> Decimal:
    """Price one OpenAI vision call from its reported usage, falling back to an estimate.

    Args:
        model: The model the call actually used.
        input_tokens: Provider-reported prompt tokens, or None.
        output_tokens: Provider-reported completion tokens, or None.

    Returns:
        The estimated dollar cost of the call.
    """
    from urbanlens.dashboard.services.ai.openai import OpenAIGateway

    fallback_sent, fallback_received = _OPENAI_VISION_FALLBACK_TOKENS
    sent = input_tokens or fallback_sent
    received = output_tokens or fallback_received
    cost_sent, cost_received = OpenAIGateway.MODEL_COSTS.get(model, OpenAIGateway.DEFAULT_COST_PER_THOUSAND)
    return (Decimal(sent) * cost_sent + Decimal(received) * cost_received) / 1000


def _describe(image_bytes: bytes, prompt: str, *, service_key: str, max_tokens: int) -> str | None:
    """Ask the configured vision provider one question about one image.

    Args:
        image_bytes: JPEG bytes, already downscaled.
        prompt: The instruction to send alongside the image.
        service_key: Rate-limit/cost bucket to account the call under.
        max_tokens: Response budget.

    Returns:
        The model's raw text answer, or None when the call failed (logged).
    """
    from urbanlens.dashboard.services.ai.inference_client import ImagePart, InferenceError, InferenceRequest, Message, TextPart, get_inference_client

    provider, model = _vision_target()
    image = ImagePart(media_type="image/jpeg", data=base64.b64encode(image_bytes).decode("ascii"))
    request = InferenceRequest(
        provider=provider,
        model=model,
        messages=[Message(role="user", content=[TextPart(text=prompt), image])],
        max_tokens=max_tokens,
    )

    started = time.monotonic()
    try:
        response = get_inference_client().send(request)
    except InferenceError:
        logger.exception("AI vision call failed (provider=%s, model=%s)", provider, model)
        log_api_call(service_key, success=False)
        return None
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Priced from the provider's own token counts where it reports them -
    # more accurate than the flat ServiceDefaults.cost_per_call the HTTP
    # gateway wrapper applies elsewhere, so it is worth storing. Cloudflare
    # Workers AI bills per request rather than per token, so it records no
    # estimate at all instead of a fabricated one.
    cost_estimate = _openai_cost(model, response.usage.input_tokens, response.usage.output_tokens) if provider == "openai" else None
    log_api_call(service_key, success=True, response_ms=elapsed_ms, endpoint=f"{provider}:{model}", cost_estimate=cost_estimate)
    logger.info("AI vision via %s %s: est. $%s, %dms", provider, model, round(cost_estimate, 5) if cost_estimate is not None else "n/a", elapsed_ms)
    return response.text


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
    if not _rate_limit_gate(SERVICE_AI_PHOTO_KEYWORDS):
        return []
    answer = _describe(image_bytes, _KEYWORD_PROMPT, service_key=SERVICE_AI_PHOTO_KEYWORDS, max_tokens=_KEYWORD_MAX_TOKENS)
    return [] if answer is None else _parse_keyword_text(answer)


def classify_photo(image_bytes: bytes) -> list[tuple[str, float]]:
    """Classify a (downscaled) photo's content via Cloudflare's ResNet-50 model.

    Unlike :func:`describe_photo_keywords` this is not a chat completion - no
    prompt, no tokens - so it takes the inference service's separate classify
    call. Cloudflare is the only provider offering one, so the site's
    ``ai_provider`` setting does not apply here.

    Args:
        image_bytes: JPEG bytes, already downscaled.

    Returns:
        (label, confidence) pairs, highest confidence first; empty on failure.
    """
    from urbanlens.dashboard.services.ai.inference_client import ClassifyRequest, ImagePart, InferenceError, get_inference_client

    if not _rate_limit_gate(SERVICE_PHOTO_CLASSIFIER):
        return []

    request = ClassifyRequest(
        provider="cloudflare",
        model=_CF_CLASSIFIER_MODEL,
        image=ImagePart(media_type="image/jpeg", data=base64.b64encode(image_bytes).decode("ascii")),
    )

    started = time.monotonic()
    try:
        response = get_inference_client().classify(request)
    except InferenceError:
        logger.exception("Photo classification failed")
        log_api_call(SERVICE_PHOTO_CLASSIFIER, success=False)
        return []
    elapsed_ms = int((time.monotonic() - started) * 1000)

    log_api_call(SERVICE_PHOTO_CLASSIFIER, success=True, response_ms=elapsed_ms, endpoint=f"cloudflare:{_CF_CLASSIFIER_MODEL}")
    return [(label.label, label.score) for label in response.labels]
