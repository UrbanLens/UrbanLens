"""One ``ApiCallLog`` row for each AI call: provider, model, status, latency, tokens and cost.

Never the prompt, an image or the answer. UrbanLens holds private and end-to-end-encrypted
content, and the ledger is read by site admins and shown on the costs page.

A call made inside an :func:`~urbanlens.dashboard.services.core.rate_limiter.api_call_slot` already
has its row - the reservation. It fills that row in rather than writing a second. A call made
outside one writes its own through ``log_api_call``; nothing here reserves, limits or refuses.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.core.rate_limiter import current_call_slot, log_api_call

if TYPE_CHECKING:
    from collections.abc import Iterator
    from decimal import Decimal

    from urbanlens.dashboard.services.ai.inference_client import InferenceError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AiCall:
    """What an AI call reports about itself for its row.

    Attributes:
        success: Whether the provider answered.
        status_code: The HTTP status of a failed call, when it had one.
        input_tokens: Prompt tokens the provider reported.
        output_tokens: Completion tokens the provider reported.
        cost_estimate: Estimated USD cost of this call, when the model is priced.
    """

    success: bool = False
    status_code: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate: Decimal | None = None


def failure_status(exc: InferenceError) -> int | None:
    """The status worth recording for a failed inference call.

    Only a 5xx: ai-inference answers 502 when the provider call failed. A 4xx is this deployment's own
    request or credential refused by ai-inference, not the provider's word, and ``provider_health``
    reads a recorded 401 as the provider refusing and a 404 as it answering with nothing.

    Args:
        exc: The failure.

    Returns:
        The HTTP status, or None.
    """
    return exc.status_code if exc.status_code is not None and exc.status_code >= 500 else None


def ai_endpoint(provider: str, model: str) -> str:
    """How an AI call's row names what was called.

    Args:
        provider: The provider, e.g. ``"cloudflare"``.
        model: The model id.

    Returns:
        ``provider:model``.
    """
    return f"{provider}:{model}"


@contextmanager
def recorded_ai_call(*, service: str, provider: str, model: str) -> Iterator[AiCall]:
    """Time one AI call and record it, however it ends.

    A failure to record is logged and swallowed: it never fails the call, and a call that raised is
    recorded as failed before the exception goes on.

    Args:
        service: The service key the row is written under (the AI feature).
        provider: The provider the call goes to.
        model: The model the call asks for.

    Yields:
        The call's report; fill it in as the answer arrives.
    """
    call = AiCall()
    started = time.monotonic()
    try:
        yield call
    finally:
        _record(service, provider, model, call, int((time.monotonic() - started) * 1000))


def _record(service: str, provider: str, model: str, call: AiCall, response_ms: int) -> None:
    try:
        endpoint = ai_endpoint(provider, model)
        slot = current_call_slot()
        # Only the slot reserved for this service: a call under another feature's slot is its own call.
        if slot is not None and slot.service == service:
            slot.endpoint, slot.model = endpoint, model
            slot.input_tokens, slot.output_tokens = call.input_tokens, call.output_tokens
            if call.status_code is not None:
                slot.status_code = call.status_code
            return
        log_api_call(
            service,
            success=call.success,
            response_ms=response_ms,
            endpoint=endpoint,
            cost_estimate=call.cost_estimate,
            status_code=call.status_code,
            model=model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
        )
    except Exception:
        logger.exception("Failed to record an AI call to %s", service)
