"""Constants and choices for site-wide settings."""

from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


class AiProviderChoice(TextChoices):
    """AI provider options for site-wide AI features."""

    CLOUDFLARE = "cloudflare", "Cloudflare Workers AI"
    OPENAI = "openai", "OpenAI"


class SearchProviderChoice(TextChoices):
    """Web search provider options for pin search panels."""

    BRAVE = "brave", "Brave Search"
    GOOGLE = "google", "Google Custom Search"


class EnvironmentOverrideChoice(TextChoices):
    """Site environment override - falls back to ``UL_ENVIRONMENT`` when DEFAULT."""

    DEFAULT = "default", "Default (from environment variable)"
    PRODUCTION = "production", "Production"
    DEVELOPMENT = "development", "Development"
    TESTING = "testing", "Testing"
    STAGING = "staging", "Staging"

    @classmethod
    def to_environment_type(cls, value: str) -> EnvironmentTypes | None:
        """Map an override value to ``EnvironmentTypes``.

        Args:
            value: A member of this choice class (not DEFAULT).

        Returns:
            The matching ``EnvironmentTypes``, or ``None`` when ``value`` is DEFAULT
            or unrecognized.
        """
        mapping: dict[str, EnvironmentTypes] = {
            cls.PRODUCTION: EnvironmentTypes.PRODUCTION,
            cls.DEVELOPMENT: EnvironmentTypes.DEVELOPMENT,
            cls.TESTING: EnvironmentTypes.TESTING,
            cls.STAGING: EnvironmentTypes.STAGING,
        }
        return mapping.get(value)


DEFAULT_OPENAI_MODEL = "gpt-5-nano"
DEFAULT_CLOUDFLARE_MODEL = "@cf/mistral/mistral-7b-instruct-v0.1"
