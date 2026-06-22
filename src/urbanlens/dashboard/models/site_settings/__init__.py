"""Site-wide settings singleton."""

from urbanlens.dashboard.models.site_settings.meta import (
    AI_PROVIDER_CHOICES,
    AI_PROVIDER_CLOUDFLARE,
    AI_PROVIDER_OPENAI,
    DEFAULT_CLOUDFLARE_MODEL,
    DEFAULT_OPENAI_MODEL,
    SEARCH_PROVIDER_BRAVE,
    SEARCH_PROVIDER_CHOICES,
    SEARCH_PROVIDER_GOOGLE,
)
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.site_settings.queryset import SiteSettingsManager, SiteSettingsQuerySet
