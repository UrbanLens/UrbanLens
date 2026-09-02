"""The whole HTTP surface of the inference service: no framework dependency.

Flask/FastAPI/Django would each pull in more than two routes need - staying
stdlib-only is part of keeping this package Django-free and small enough to
audit in one sitting. Deployed as ``urbanlens_ai.wsgi:application`` behind
gunicorn's ``gthread`` worker (see the plan's architecture note for why
threads, not gevent: the provider SDKs' blocking HTTP calls need a worker
model that actually parks on I/O).

Logs request ids, provider, model, token counts, and latency - never prompt
or response text, and never a request body verbatim in an error response
(see ``_handle_messages``): this process sees the same content the model
does, and a log line is a much less carefully reviewed place for it to leak
into than the response itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hmac
import json
import logging
import time
from typing import Any
import uuid

from pydantic import ValidationError

from urbanlens_ai.config import get_config
from urbanlens_ai.policy import PolicyError, validate_classify_request, validate_request
from urbanlens_ai.providers import ProviderError, build_adapter
from urbanlens_ai.schema import ClassifyRequest, InferenceRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("urbanlens_ai")

# Fail fast at process start (gunicorn imports this module before serving
# any request) rather than on whatever request happens to arrive first.
get_config()

#: A text-only chat turn is a few KB. The ceiling is sized for the one case
#: that legitimately needs more - a vision call carrying a base64 image
#: (``policy.MAX_IMAGE_BYTES`` bounds the image itself, and base64 inflates
#: it by a third) - and still bounds memory in what gunicorn runs as a
#: single worker process (see the compose command: ``-w 1``).
MAX_REQUEST_BYTES = 4 * 1024 * 1024

StartResponse = Callable[[str, list[tuple[str, str]]], object]


def _json_response(start_response: StartResponse, status: str, payload: dict[str, Any]) -> Iterable[bytes]:
    body = json.dumps(payload).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]


def _authorized(environ: dict[str, Any]) -> bool:
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return False
    token = header.removeprefix("Bearer ")
    # Constant-time: this bearer is the only credential guarding a process
    # that holds every provider API key this deployment has.
    return hmac.compare_digest(token, get_config().ai_inference_token)


def _read_body(environ: dict[str, Any]) -> bytes | None:
    """The request body, or ``None`` when its declared length is missing or over the cap."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None
    if length <= 0 or length > MAX_REQUEST_BYTES:
        return None
    body: bytes = environ["wsgi.input"].read(length)
    return body


def _handle_classify(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    raw = _read_body(environ)
    if raw is None:
        return _json_response(start_response, "400 Bad Request", {"error": "invalid content length"})
    try:
        request = ClassifyRequest.model_validate_json(raw)
    except (ValidationError, ValueError):
        return _json_response(start_response, "400 Bad Request", {"error": "invalid request body"})

    try:
        validate_classify_request(request)
    except PolicyError as exc:
        return _json_response(start_response, "400 Bad Request", {"error": str(exc)})

    request_id = uuid.uuid4().hex
    started = time.monotonic()
    try:
        adapter = build_adapter(request.provider, get_config())
        response = adapter.classify(request)
    except ProviderError as exc:
        logger.warning("request=%s provider=%s model=%s classify failed after %.2fs: %s", request_id, request.provider, request.model, time.monotonic() - started, exc)
        return _json_response(start_response, "502 Bad Gateway", {"error": "provider call failed"})

    logger.info("request=%s provider=%s model=%s labels=%d latency_ms=%d", request_id, request.provider, request.model, len(response.labels), int((time.monotonic() - started) * 1000))
    return _json_response(start_response, "200 OK", response.model_dump(mode="json"))


def _handle_messages(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    raw = _read_body(environ)
    if raw is None:
        return _json_response(start_response, "400 Bad Request", {"error": "invalid content length"})
    try:
        request = InferenceRequest.model_validate_json(raw)
    except (ValidationError, ValueError):
        # Never echo the parsed exception: pydantic's message can include the
        # offending field value, which may be prompt/message content.
        return _json_response(start_response, "400 Bad Request", {"error": "invalid request body"})

    try:
        validate_request(request)
    except PolicyError as exc:
        return _json_response(start_response, "400 Bad Request", {"error": str(exc)})

    request_id = uuid.uuid4().hex
    started = time.monotonic()
    try:
        adapter = build_adapter(request.provider, get_config())
        response = adapter.send(request)
    except ProviderError as exc:
        logger.warning(
            "request=%s provider=%s model=%s failed after %.2fs: %s",
            request_id,
            request.provider,
            request.model,
            time.monotonic() - started,
            exc,
        )
        return _json_response(start_response, "502 Bad Gateway", {"error": "provider call failed"})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "request=%s provider=%s model=%s stop_reason=%s input_tokens=%s output_tokens=%s latency_ms=%d",
        request_id,
        request.provider,
        request.model,
        response.stop_reason,
        response.usage.input_tokens,
        response.usage.output_tokens,
        elapsed_ms,
    )
    return _json_response(start_response, "200 OK", response.model_dump(mode="json"))


def application(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    """The whole HTTP surface: ``GET /health/``, ``POST /v1/messages``, ``POST /v1/classify``."""
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "")

    if path == "/health/" and method == "GET":
        return _json_response(start_response, "200 OK", {"status": "ok"})

    if path in {"/v1/messages", "/v1/classify"} and method == "POST":
        if not _authorized(environ):
            return _json_response(start_response, "401 Unauthorized", {"error": "unauthorized"})
        if path == "/v1/classify":
            return _handle_classify(environ, start_response)
        return _handle_messages(environ, start_response)

    return _json_response(start_response, "404 Not Found", {"error": "not found"})
