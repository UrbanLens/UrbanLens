"""HTTP (and, for local dev, in-process) client for the AI inference service.

The one module under ``urbanlens`` that imports ``urbanlens_ai`` - see that
package's own docstring for the import-direction rule this keeps (nothing
under ``urbanlens_ai`` may import back).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

import requests

# Re-exported so gateway.py (and anything else under `dashboard`) can build
# requests without importing urbanlens_ai directly - this module is the one
# permitted import site (see the module docstring).
from urbanlens_ai.schema import InferenceRequest, InferenceResponse, Message, Provider, ToolSpec

if TYPE_CHECKING:
    from urbanlens_ai.config import InferenceConfig

__all__ = [
    "InferenceClient",
    "InferenceError",
    "InferenceRequest",
    "InferenceResponse",
    "LocalInferenceClient",
    "Message",
    "Provider",
    "RemoteInferenceClient",
    "ToolSpec",
    "get_inference_client",
]

logger = logging.getLogger(__name__)


class InferenceError(RuntimeError):
    """The inference call failed: network error, HTTP error, or an unparseable response."""


class InferenceClient(Protocol):
    """Sends a normalized :class:`InferenceRequest` somewhere and returns the answer."""

    def send(self, request: InferenceRequest) -> InferenceResponse: ...


class RemoteInferenceClient:
    """Calls the sandboxed ``ai-inference`` service over HTTP.

    Used whenever ``UL_AI_INFERENCE_URL`` is configured - every staging/
    production deployment, and any local dev setup that opts into running
    the sandbox stack. Presents a bearer token; holds no provider credential
    of its own.
    """

    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    def send(self, request: InferenceRequest) -> InferenceResponse:
        try:
            response = requests.post(
                f"{self._base_url}/v1/messages",
                json=request.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise InferenceError(f"ai-inference request failed: {exc}") from exc

        if response.status_code != 200:
            raise InferenceError(f"ai-inference returned HTTP {response.status_code}")

        try:
            return InferenceResponse.model_validate(response.json())
        except ValueError as exc:
            raise InferenceError(f"ai-inference returned an unparseable response: {exc}") from exc


class LocalInferenceClient:
    """Calls the provider SDKs in-process - local dev/tests only.

    Bypasses the sandbox entirely (provider keys come from *this* process's
    own settings), so :func:`~services.sandbox.guard.check_direct_inference`
    must pass before every call - see that guard for the exact policy. This
    is the fallback ``get_gateway()`` picks when ``UL_AI_INFERENCE_URL`` is
    unset, so a plain local checkout with no sandbox stack running still
    works exactly as the pre-sandbox code did.
    """

    def send(self, request: InferenceRequest) -> InferenceResponse:
        from urbanlens.dashboard.services.sandbox.guard import check_direct_inference

        check_direct_inference()

        from urbanlens_ai.policy import PolicyError, validate_request
        from urbanlens_ai.providers import ProviderError, build_adapter

        try:
            validate_request(request)
            adapter = build_adapter(request.provider, self._build_config())
            return adapter.send(request)
        except (PolicyError, ProviderError) as exc:
            # Normalized to InferenceError so callers (gateway.py) only ever
            # handle one exception type regardless of which client ran -
            # urbanlens_ai's own exception types stay inside this module.
            raise InferenceError(str(exc)) from exc

    def _build_config(self) -> InferenceConfig:
        """Build an ``InferenceConfig`` from this process's own Django settings.

        ``urbanlens_ai.config.InferenceConfig`` normally reads straight from
        the process environment (see that module) - correct for its real
        deployment, which gets real container env vars and no ``.env`` file.
        A local checkout may instead have these set only in ``.env``
        (loaded by Django's pydantic ``AppSettings``, not by this process's
        raw environment), so this constructs the config explicitly from
        ``AppSettings`` instead of re-reading the environment independently.
        """
        from urbanlens.UrbanLens.settings.app import settings
        from urbanlens_ai.config import InferenceConfig

        return InferenceConfig(
            # Never read by this path (no HTTP hop, nothing to authenticate) -
            # required only because InferenceConfig also serves the real
            # ai-inference service, where it is load-bearing.
            ai_inference_token="unused-direct-in-process-call",  # noqa: S106 -- placeholder, not a credential; see comment above
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
            cloudflare_ai_api_key=settings.cloudflare_ai_api_key,
            cloudflare_worker_ai_endpoint=str(settings.cloudflare_worker_ai_endpoint) if settings.cloudflare_worker_ai_endpoint else None,
        )


def get_inference_client() -> InferenceClient:
    """Pick the client every :class:`~services.ai.gateway.LLMGateway` sends through.

    Remote whenever ``UL_AI_INFERENCE_URL`` is configured (every staging/
    production deployment); local otherwise, so a plain checkout with no
    sandbox stack running still works.
    """
    from urbanlens.UrbanLens.settings.app import settings

    if settings.ai_inference_url:
        return RemoteInferenceClient(
            base_url=settings.ai_inference_url,
            token=settings.ai_inference_token or "",
            timeout_seconds=settings.ai_inference_timeout_seconds,
        )
    return LocalInferenceClient()
