"""Site-wide settings singleton."""

from urbanlens.dashboard.models.site_settings.meta import (
    AiProviderChoice,
    DEFAULT_CLOUDFLARE_MODEL,
    DEFAULT_OPENAI_MODEL,
    EnvironmentOverrideChoice,
    SearchProviderChoice,
)
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.site_settings.queryset import SiteSettingsManager, SiteSettingsQuerySet
