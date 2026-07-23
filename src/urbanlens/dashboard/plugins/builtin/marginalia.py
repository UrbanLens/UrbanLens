"""Marginalia plugin: independent, non-commercial web search provider."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class MarginaliaPlugin(UrbanLensPlugin):
    """Adds Marginalia as a selectable web-search provider (Settings > Search)."""

    name: ClassVar[str] = "marginalia"
    verbose_name: ClassVar[str] = "Marginalia Search"
    description: ClassVar[str] = (
        "Independent, non-commercial search engine favouring small, text-heavy, low-SEO sites - good for "
        "niche forum threads and archives about a place. Works with no configuration via a shared public "
        "testing key; set UL_MARGINALIA_API_KEY for a dedicated, non-rate-shared key."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Marginalia Search API."""
        return {
            "marginalia_search": ServiceDefaults(
                display_name="Marginalia Search API",
                calls_per_minute=10,
                calls_per_day=200,
                notes="Free. The shared 'public' key (used when UL_MARGINALIA_API_KEY is unset) has a tighter, cross-consumer rate limit - a dedicated key raises this considerably.",
            ),
        }
