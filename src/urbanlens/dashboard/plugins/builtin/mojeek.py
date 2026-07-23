"""Mojeek plugin: independent-index web search provider."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class MojeekPlugin(UrbanLensPlugin):
    """Adds Mojeek as a selectable web-search provider (Settings > Search)."""

    name: ClassVar[str] = "mojeek"
    verbose_name: ClassVar[str] = "Mojeek Search"
    description: ClassVar[str] = "Independent web index (not a Google/Bing reseller) with a free tier for low-volume use - surfaces smaller, less SEO-optimised sites the mainstream engines deprioritise."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Mojeek Search API free tier."""
        return {
            "mojeek_search": ServiceDefaults(
                display_name="Mojeek Search API",
                calls_per_minute=10,
                calls_per_day=200,
                notes="Free tier for low-volume, non-commercial use. Requires UL_MOJEEK_API_KEY.",
            ),
        }
