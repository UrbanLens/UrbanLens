"""Cloudflare Workers AI provider adapter."""

from __future__ import annotations

import base64
import re
from typing import Any

import requests

from urbanlens_ai import policy
from urbanlens_ai.providers.base import ProviderAdapter, ProviderError, ProviderInputRefusedError
from urbanlens_ai.schema import ClassificationLabel, ClassifyRequest, ClassifyResponse, InferenceRequest, InferenceResponse, TextBlock, Usage

#: Joins the system prompt and each turn's text into the single flat
#: ``prompt`` string Workers AI vision models take instead of a messages array.
NEWLINE = chr(10)

#: Cloudflare's 400 for an input a model will not take, as opposed to a request this adapter got wrong (an unknown
#: model, a payload shape the model no longer accepts), which stays a failure. ResNet-50 answers code 3011,
#: ``AiError: image too small, expected image at least 4x4``.
_REFUSED_INPUT = re.compile(r"image too small", re.IGNORECASE)

#: Most of an error body kept in an exception message.
_ERROR_DETAIL_CHARS = 300


def _error_detail(response: requests.Response) -> str:
    """Cloudflare's own ``code: message`` pairs from an error body, or the HTTP reason when it has none.

    Args:
        response: A non-2xx Workers AI response.

    Returns:
        The provider's reason for refusing, without the request URL (it names the account).
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list):
        detail = "; ".join(f"{error.get('code')}: {error.get('message')}" for error in errors if isinstance(error, dict))
        if detail:
            return detail[:_ERROR_DETAIL_CHARS]
    return response.reason or "no reason given"


class CloudflareAdapter(ProviderAdapter):
    """Translates :class:`InferenceRequest` to and from Cloudflare's Workers AI REST API."""

    def __init__(self, api_key: str, endpoint: str) -> None:
        policy.validate_cloudflare_endpoint(endpoint)
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")

    def _post(self, model: str, payload: dict[str, Any], *, timeout: float = policy.PROVIDER_TIMEOUT_SECONDS) -> dict[str, Any]:
        """POST one Workers AI request and return its parsed JSON body."""
        url = f"{self._endpoint}/{model.lstrip('/')}"
        try:
            response = requests.post(url, headers={"Authorization": f"Bearer {self._api_key}"}, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"Cloudflare Workers AI call failed: {exc}") from exc
        if not response.ok:
            detail = _error_detail(response)
            if response.status_code == 400 and _REFUSED_INPUT.search(detail):
                raise ProviderInputRefusedError(f"Cloudflare Workers AI refused the input (HTTP 400): {detail}")
            raise ProviderError(f"Cloudflare Workers AI answered HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(f"Cloudflare Workers AI returned unparseable JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ProviderError(f"Cloudflare Workers AI returned a non-object body: {body!r}")
        return body

    def send(self, request: InferenceRequest) -> InferenceResponse:
        images = [image for message in request.messages for image in message.images]
        if images:
            # Workers AI's vision models take a flat {image, prompt} payload,
            # not a messages array - the image is a byte array rather than a
            # data URL, and there is no multi-turn shape to put it in. Only
            # the last image and the whole conversation's text are sent;
            # every shipped vision caller is a single-turn, single-image ask.
            prompt = NEWLINE.join(part for part in ([request.system] + [message.text for message in request.messages]) if part)
            payload: dict[str, Any] = {"image": list(base64.b64decode(images[-1].data)), "prompt": prompt, "max_tokens": request.max_tokens}
        else:
            messages: list[dict[str, str]] = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.extend({"role": message.role, "content": message.text} for message in request.messages)
            payload = {"messages": messages}

        body = self._post(request.model, payload, timeout=policy.attempt_limits(request.timeout_seconds)[0])

        result = body.get("result")
        if not isinstance(result, dict):
            raise ProviderError(f"Cloudflare Workers AI response missing result: {body!r}")
        # Vision models answer under "description"; text models under
        # "response". Accept either rather than branching on which call this
        # was - the model, not this adapter, decides which key it fills.
        text = result.get("response") or result.get("description")
        if text is None:
            raise ProviderError(f"Cloudflare Workers AI response missing result.response: {body!r}")
        text = str(text)

        # Workers AI does not consistently report token usage across models;
        # the caller falls back to its own estimate when both fields are None.
        usage = Usage()
        if isinstance(result.get("usage"), dict):
            raw_usage = result["usage"]
            usage = Usage(input_tokens=raw_usage.get("prompt_tokens"), output_tokens=raw_usage.get("completion_tokens"))

        return InferenceResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=usage)

    def classify(self, request: ClassifyRequest) -> ClassifyResponse:
        """Run one Workers AI image classifier (e.g. ResNet-50) and normalize its labels."""
        body = self._post(request.model, {"image": list(base64.b64decode(request.image.data))})
        labels: list[ClassificationLabel] = []
        for entry in body.get("result") or []:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            if not label:
                continue
            try:
                score = float(entry.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            labels.append(ClassificationLabel(label=label, score=score))
        labels.sort(key=lambda item: item.score, reverse=True)
        return ClassifyResponse(labels=labels)
