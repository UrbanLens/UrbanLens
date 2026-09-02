"""Environment-only configuration for the inference service - no Django, no .env file.

``docker-compose.yml`` sets these as real container environment variables
directly (no ``env_file:`` on this service - see the plan's credentials
matrix: this is the one container that must never see the host's full
``.env``), so this reads straight from the process environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class InferenceConfig(BaseSettings):
    """``UL_``-prefixed environment variables this service reads.

    Field names deliberately match ``UrbanLens.settings.app.AppSettings``'s
    provider-key field names one-for-one (same ``UL_`` prefix, same suffix),
    since it is the same credential moving to a different container - not a
    new one.
    """

    #: Shared bearer secret every caller must present. An empty default
    #: (rather than a required field with none, which pydantic-settings'
    #: dynamic ``__init__`` gives mypy no static way to see is actually
    #: populated from the environment) plus the validator below: an
    #: inference service that would accept requests with none configured is
    #: worse than one that refuses to start.
    ai_inference_token: str = Field(default="")
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    cloudflare_ai_api_key: str | None = None
    cloudflare_worker_ai_endpoint: str | None = None

    model_config = SettingsConfigDict(env_prefix="UL_", str_strip_whitespace=True, env_ignore_empty=True, extra="ignore")

    @field_validator("ai_inference_token")
    @classmethod
    def _require_token(cls, value: str) -> str:
        if not value:
            raise ValueError("UL_AI_INFERENCE_TOKEN is required")
        return value


@lru_cache(maxsize=1)
def get_config() -> InferenceConfig:
    """Cached singleton - re-reading the environment per request buys nothing."""
    return InferenceConfig()
